/*
 * rknn_detect.c
 *
 * RKNN NPU inference wrapper for YOLOv5n INT8 on RV1106G3.
 *
 * Uses the RKNN C API with zero-copy memory management, which is the
 * required approach on RV1103/RV1106 platforms. The runtime library is
 * librknnmrt.so (mini runtime).
 *
 * API call flow:
 *   1. rknn_init()           - Load model
 *   2. rknn_query()          - Query I/O attributes
 *   3. rknn_create_mem()     - Allocate zero-copy tensor memory
 *   4. rknn_set_io_mem()     - Bind memory to I/O tensors
 *   5. memcpy to input_mem   - Fill input data
 *   6. rknn_run()            - Execute NPU inference
 *   7. Read from output_mem  - Access results directly (zero-copy)
 *   8. rknn_destroy_mem()    - Cleanup
 *   9. rknn_destroy()        - Release context
 *
 * Reference: rknpu2 RV1106_RV1103/rknn_yolov5_demo (airockchip/rknn-toolkit2)
 */

#include "rknn_detect.h"
#include "postprocess.h"
#include "hud_ipc.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/time.h>
#include <unistd.h>
#include <errno.h>

/* RKNN C API header */
#include "rknn_api.h"

/* RKMPI headers for frame grabbing in the inference thread */
#include "rk_mpi_vi.h"
#include "rk_mpi_vpss.h"
#include "rk_mpi_mb.h"

/* --------------------------------------------------------------------------
 * Internal helpers
 * -------------------------------------------------------------------------- */

static inline int64_t get_current_time_us(void) {
    struct timeval tv;
    gettimeofday(&tv, NULL);
    return (int64_t)tv.tv_sec * 1000000 + tv.tv_usec;
}

/*
 * NV12 to RGB888 conversion (software fallback).
 *
 * NV12 layout: Y plane (w*h) + interleaved UV plane (w*h/2)
 * Output: packed RGB888 (w*h*3)
 *
 * On RV1106, hardware RGA could be used for this conversion, but for
 * simplicity and portability we use a CPU implementation. At 480x480
 * the overhead is ~1ms on Cortex-A7.
 */
static void nv12_to_rgb(const uint8_t *nv12, uint8_t *rgb,
                         int width, int height) {
    const uint8_t *y_plane = nv12;
    const uint8_t *uv_plane = nv12 + width * height;

    for (int row = 0; row < height; row++) {
        for (int col = 0; col < width; col++) {
            int y_idx = row * width + col;
            /* UV is subsampled 2x2: each UV pair covers a 2x2 block */
            int uv_idx = (row / 2) * width + (col & ~1);

            int y_val = (int)y_plane[y_idx];
            int u_val = (int)uv_plane[uv_idx + 0] - 128;
            int v_val = (int)uv_plane[uv_idx + 1] - 128;

            /* BT.601 conversion */
            int r = y_val + ((359 * v_val) >> 8);
            int g = y_val - ((88 * u_val + 183 * v_val) >> 8);
            int b = y_val + ((454 * u_val) >> 8);

            /* Clamp to [0, 255] */
            if (r < 0) r = 0; else if (r > 255) r = 255;
            if (g < 0) g = 0; else if (g > 255) g = 255;
            if (b < 0) b = 0; else if (b > 255) b = 255;

            int rgb_idx = y_idx * 3;
            rgb[rgb_idx + 0] = (uint8_t)r;
            rgb[rgb_idx + 1] = (uint8_t)g;
            rgb[rgb_idx + 2] = (uint8_t)b;
        }
    }
}

/*
 * Nearest-neighbor RGB888 resize.
 *
 * Used when the VPSS output size doesn't match the model input size
 * (e.g. VPSS CHN0 outputs 480x480 but model expects 640x640).
 * Integer-only arithmetic for ARM Cortex-A7 efficiency.
 */
static void resize_rgb_nearest(const uint8_t *src, int src_w, int src_h,
                                uint8_t *dst, int dst_w, int dst_h) {
    for (int dy = 0; dy < dst_h; dy++) {
        int sy = dy * src_h / dst_h;
        const uint8_t *src_row = src + sy * src_w * 3;
        uint8_t *dst_row = dst + dy * dst_w * 3;

        for (int dx = 0; dx < dst_w; dx++) {
            int sx = dx * src_w / dst_w;
            const uint8_t *sp = src_row + sx * 3;
            uint8_t *dp = dst_row + dx * 3;
            dp[0] = sp[0];
            dp[1] = sp[1];
            dp[2] = sp[2];
        }
    }
}

/* --------------------------------------------------------------------------
 * Inference thread context (passed via void* arg)
 * -------------------------------------------------------------------------- */

typedef struct _infer_thread_arg_t {
    rknn_detect_ctx_t *ctx;
    int src_type;   /* RKNN_SRC_VI or RKNN_SRC_VPSS */
    int dev_id;     /* VI pipe or VPSS group */
    int chn_id;     /* VI/VPSS channel */
} infer_thread_arg_t;

/* --------------------------------------------------------------------------
 * rknn_detect_init
 * -------------------------------------------------------------------------- */

int rknn_detect_init(rknn_detect_ctx_t *ctx, const char *model_path) {
    if (!ctx) return -1;

    memset(ctx, 0, sizeof(rknn_detect_ctx_t));
    pthread_mutex_init(&ctx->result_mutex, NULL);

    const char *path = model_path ? model_path : RKNN_MODEL_PATH_DEFAULT;
    int ret;

    /* ---- Step 1: Initialize RKNN context from model file ---- */
    rknn_context rknn_ctx = 0;
    ret = rknn_init(&rknn_ctx, (void *)path, 0, 0, NULL);
    if (ret < 0) {
        printf("[ERROR] rknn_init failed: ret=%d, model=%s\n", ret, path);
        return -1;
    }
    ctx->rknn_ctx = rknn_ctx;
    printf("[INFO] RKNN model loaded: %s\n", path);

    /* ---- Step 2: Query SDK version ---- */
    rknn_sdk_version sdk_ver;
    ret = rknn_query(rknn_ctx, RKNN_QUERY_SDK_VERSION, &sdk_ver, sizeof(sdk_ver));
    if (ret == 0) {
        printf("[INFO] RKNN API version: %s, driver version: %s\n",
               sdk_ver.api_version, sdk_ver.drv_version);
    }

    /* ---- Step 3: Query I/O tensor count ---- */
    rknn_input_output_num io_num;
    ret = rknn_query(rknn_ctx, RKNN_QUERY_IN_OUT_NUM, &io_num, sizeof(io_num));
    if (ret != 0) {
        printf("[ERROR] rknn_query IN_OUT_NUM failed: ret=%d\n", ret);
        goto fail;
    }
    ctx->n_input  = io_num.n_input;
    ctx->n_output = io_num.n_output;
    printf("[INFO] Model I/O: %d input(s), %d output(s)\n",
           io_num.n_input, io_num.n_output);

    if (io_num.n_input != 1 || io_num.n_output != 3) {
        printf("[ERROR] Expected 1 input and 3 outputs, got %d/%d\n",
               io_num.n_input, io_num.n_output);
        goto fail;
    }

    /* ---- Step 4: Query input tensor attributes ---- */
    rknn_tensor_attr input_attr;
    memset(&input_attr, 0, sizeof(input_attr));
    input_attr.index = 0;
    ret = rknn_query(rknn_ctx, RKNN_QUERY_INPUT_ATTR, &input_attr,
                     sizeof(rknn_tensor_attr));
    if (ret != 0) {
        printf("[ERROR] rknn_query INPUT_ATTR failed: ret=%d\n", ret);
        goto fail;
    }

    /* Parse input dimensions (NHWC or NCHW) */
    if (input_attr.fmt == RKNN_TENSOR_NHWC) {
        ctx->model_height  = input_attr.dims[1];
        ctx->model_width   = input_attr.dims[2];
        ctx->model_channel = input_attr.dims[3];
    } else {
        /* NCHW */
        ctx->model_channel = input_attr.dims[1];
        ctx->model_height  = input_attr.dims[2];
        ctx->model_width   = input_attr.dims[3];
    }
    ctx->input_w_stride = input_attr.w_stride;

    printf("[INFO] Model input: %dx%dx%d, fmt=%d, type=%d, stride=%d\n",
           ctx->model_width, ctx->model_height, ctx->model_channel,
           input_attr.fmt, input_attr.type, input_attr.w_stride);

    /* ---- Step 5: Query output tensor attributes ---- */
    rknn_tensor_attr output_attrs[3];
    for (uint32_t i = 0; i < 3; i++) {
        memset(&output_attrs[i], 0, sizeof(rknn_tensor_attr));
        output_attrs[i].index = i;

        /*
         * On RV1106/RV1103, use RKNN_QUERY_NATIVE_NHWC_OUTPUT_ATTR to get
         * output in NHWC layout directly. This avoids NC1HWC2 format
         * conversion overhead.
         */
        ret = rknn_query(rknn_ctx, RKNN_QUERY_NATIVE_NHWC_OUTPUT_ATTR,
                         &output_attrs[i], sizeof(rknn_tensor_attr));
        if (ret != 0) {
            printf("[ERROR] rknn_query OUTPUT_ATTR[%d] failed: ret=%d\n", i, ret);
            goto fail;
        }

        ctx->out_zps[i]    = output_attrs[i].zp;
        ctx->out_scales[i] = output_attrs[i].scale;

        printf("[INFO] Output[%d]: dims=[%d,%d,%d,%d], type=%d, "
               "zp=%d, scale=%.6f\n",
               i,
               output_attrs[i].dims[0], output_attrs[i].dims[1],
               output_attrs[i].dims[2], output_attrs[i].dims[3],
               output_attrs[i].type,
               output_attrs[i].zp, output_attrs[i].scale);
    }

    /* ---- Step 6: Create zero-copy tensor memory ---- */

    /* Input memory */
    /*
     * Override input type/format: we provide UINT8 RGB data in NHWC layout.
     * The RKNN runtime handles quantization internally when pass_through=0.
     */
    input_attr.type = RKNN_TENSOR_UINT8;
    input_attr.fmt  = RKNN_TENSOR_NHWC;

    rknn_tensor_mem *input_mem = rknn_create_mem(rknn_ctx,
                                                  input_attr.size_with_stride);
    if (!input_mem) {
        printf("[ERROR] rknn_create_mem for input failed\n");
        goto fail;
    }
    ctx->input_mem = input_mem;

    /* Bind input memory */
    ret = rknn_set_io_mem(rknn_ctx, input_mem, &input_attr);
    if (ret < 0) {
        printf("[ERROR] rknn_set_io_mem for input failed: ret=%d\n", ret);
        goto fail;
    }

    /* Output memory (3 detection heads) */
    for (uint32_t i = 0; i < 3; i++) {
        rknn_tensor_mem *out_mem = rknn_create_mem(rknn_ctx,
                                                    output_attrs[i].size_with_stride);
        if (!out_mem) {
            printf("[ERROR] rknn_create_mem for output[%d] failed\n", i);
            goto fail;
        }
        ctx->output_mem[i] = out_mem;

        ret = rknn_set_io_mem(rknn_ctx, out_mem, &output_attrs[i]);
        if (ret < 0) {
            printf("[ERROR] rknn_set_io_mem for output[%d] failed: ret=%d\n",
                   i, ret);
            goto fail;
        }
    }

    /* ---- Step 7: Allocate NV12-to-RGB conversion buffer ---- */
    ctx->rgb_buf = (uint8_t *)malloc(ctx->model_width * ctx->model_height * 3);
    if (!ctx->rgb_buf) {
        printf("[ERROR] Failed to allocate RGB conversion buffer\n");
        goto fail;
    }

    ctx->initialized = 1;
    printf("[INFO] RKNN detector initialized successfully\n");
    return 0;

fail:
    rknn_detect_release(ctx);
    return -1;
}

/* --------------------------------------------------------------------------
 * rknn_detect_run
 * -------------------------------------------------------------------------- */

int rknn_detect_run(rknn_detect_ctx_t *ctx,
                    const uint8_t *nv12_data, int width, int height,
                    detect_result_group_t *group) {
    if (!ctx || !ctx->initialized || !nv12_data || !group)
        return -1;

    int model_w = ctx->model_width;
    int model_h = ctx->model_height;

    int ret;
    int64_t t0, t1, t2;

    /* ---- Convert NV12 to RGB888 (with optional resize) ---- */
    if (width == model_w && height == model_h) {
        /* Fast path: frame matches model input -- direct conversion */
        nv12_to_rgb(nv12_data, ctx->rgb_buf, width, height);
    } else {
        /*
         * Resize path: frame size differs from model input.
         * 1) NV12->RGB at source resolution
         * 2) Nearest-neighbor resize to model input size
         *
         * Typical case: VPSS CHN0 outputs 480x480, model expects 640x640.
         */
        int needed = width * height * 3;
        if (!ctx->frame_rgb_buf ||
            ctx->frame_buf_w != width || ctx->frame_buf_h != height) {
            free(ctx->frame_rgb_buf);
            ctx->frame_rgb_buf = (uint8_t *)malloc(needed);
            if (!ctx->frame_rgb_buf) {
                printf("[ERROR] Failed to allocate frame RGB buffer (%dx%d)\n",
                       width, height);
                return -1;
            }
            ctx->frame_buf_w = width;
            ctx->frame_buf_h = height;
            printf("[INFO] Allocated frame RGB buffer: %dx%d -> %dx%d resize\n",
                   width, height, model_w, model_h);
        }
        nv12_to_rgb(nv12_data, ctx->frame_rgb_buf, width, height);
        resize_rgb_nearest(ctx->frame_rgb_buf, width, height,
                           ctx->rgb_buf, model_w, model_h);
    }

    /* ---- Copy RGB data into input tensor memory ---- */
    rknn_tensor_mem *input_mem = (rknn_tensor_mem *)ctx->input_mem;
    int model_ch = ctx->model_channel;
    uint32_t stride = ctx->input_w_stride;

    if (stride == 0 || stride == (uint32_t)model_w) {
        /* No padding, straight copy */
        memcpy(input_mem->virt_addr, ctx->rgb_buf,
               model_w * ctx->model_height * model_ch);
    } else {
        /* Handle width stride padding: copy row by row */
        uint8_t *src = ctx->rgb_buf;
        uint8_t *dst = (uint8_t *)input_mem->virt_addr;
        int src_row_bytes = model_w * model_ch;
        int dst_row_bytes = stride * model_ch;
        for (int row = 0; row < ctx->model_height; row++) {
            memcpy(dst, src, src_row_bytes);
            src += src_row_bytes;
            dst += dst_row_bytes;
        }
    }

    /* ---- Run NPU inference ---- */
    t0 = get_current_time_us();

    ret = rknn_run((rknn_context)ctx->rknn_ctx, NULL);
    if (ret < 0) {
        printf("[ERROR] rknn_run failed: ret=%d\n", ret);
        return -1;
    }

    t1 = get_current_time_us();

    /* ---- Post-processing ---- */
    /*
     * Output tensors are already in zero-copy memory. Access directly
     * via output_mem[i]->virt_addr.
     *
     * scale_w/scale_h map detected box coordinates from model space back
     * to the original frame space. When frame == model size, both are 1.0.
     * When resized (e.g. 480->640), scale = model_dim / frame_dim.
     *
     * Note: For HUD display we only use class_id and confidence, so
     * exact box coordinates are not critical.
     */
    float scale_w = (float)model_w / (float)width;
    float scale_h = (float)model_h / (float)height;

    rknn_tensor_mem *out0 = (rknn_tensor_mem *)ctx->output_mem[0];
    rknn_tensor_mem *out1 = (rknn_tensor_mem *)ctx->output_mem[1];
    rknn_tensor_mem *out2 = (rknn_tensor_mem *)ctx->output_mem[2];

    ret = post_process((int8_t *)out0->virt_addr,
                       (int8_t *)out1->virt_addr,
                       (int8_t *)out2->virt_addr,
                       ctx->model_height, ctx->model_width,
                       BOX_THRESH, NMS_THRESH,
                       scale_w, scale_h,
                       ctx->out_zps, ctx->out_scales,
                       group);

    t2 = get_current_time_us();

    /* Update performance counters */
    ctx->last_infer_ms    = (float)(t1 - t0) / 1000.0f;
    ctx->last_postproc_ms = (float)(t2 - t1) / 1000.0f;
    ctx->total_frames++;
    ctx->total_detections += (uint64_t)group->count;

    return ret;
}

/* --------------------------------------------------------------------------
 * Inference thread
 * -------------------------------------------------------------------------- */

static void *inference_thread_func(void *arg) {
    infer_thread_arg_t *targ = (infer_thread_arg_t *)arg;
    rknn_detect_ctx_t *ctx = targ->ctx;
    int src_type = targ->src_type;
    int dev_id   = targ->dev_id;
    int chn_id   = targ->chn_id;
    free(targ);  /* Allocated by rknn_detect_start_thread */

    const char *src_name = (src_type == RKNN_SRC_VI) ? "VI" : "VPSS";
    printf("[INFO] RKNN inference thread started (%s dev=%d, chn=%d)\n",
           src_name, dev_id, chn_id);

    int consecutive_errors = 0;
    detect_result_group_t local_result;

    while (!ctx->thread_should_stop) {
        /*
         * Grab a frame (NV12) from the configured source.
         *
         * VI mode:   RK_MPI_VI_GetChnFrame (dedicated ISP selfpath channel)
         * VPSS mode: RK_MPI_VPSS_GetChnFrame (fallback, limited by bind)
         */
        VIDEO_FRAME_INFO_S frame_info;
        memset(&frame_info, 0, sizeof(frame_info));

        int ret;
        if (src_type == RKNN_SRC_VI) {
            ret = RK_MPI_VI_GetChnFrame(dev_id, chn_id,
                                         &frame_info, RKNN_FRAME_TIMEOUT_MS);
        } else {
            ret = RK_MPI_VPSS_GetChnFrame(dev_id, chn_id,
                                           &frame_info, RKNN_FRAME_TIMEOUT_MS);
        }
        if (ret != 0) {
            if (!ctx->thread_should_stop) {
                consecutive_errors++;
                if (consecutive_errors <= RKNN_MAX_CONSECUTIVE_ERRORS ||
                    (consecutive_errors % 100) == 0) {
                    printf("[WARN] %s GetChnFrame failed: 0x%x (errors=%d)\n",
                           src_name, ret, consecutive_errors);
                }
            }
            continue;
        }
        consecutive_errors = 0;

        /*
         * Get the virtual address of the frame data.
         * RK_MPI_MB_Handle2VirAddr converts the RKMPI memory block handle
         * to a CPU-accessible pointer.
         */
        VIDEO_FRAME_S *vf = &frame_info.stVFrame;
        void *frame_vaddr = RK_MPI_MB_Handle2VirAddr(vf->pMbBlk);

        if (frame_vaddr) {
            /* Run inference */
            memset(&local_result, 0, sizeof(local_result));
            ret = rknn_detect_run(ctx,
                                  (const uint8_t *)frame_vaddr,
                                  vf->u32Width, vf->u32Height,
                                  &local_result);

            if (ret == 0) {
                /* Update shared result (thread-safe) */
                pthread_mutex_lock(&ctx->result_mutex);
                memcpy(&ctx->det_result, &local_result, sizeof(local_result));
                pthread_mutex_unlock(&ctx->result_mutex);

                /* Write detection results to IPC file for HUD display */
                if (local_result.count > 0) {
                    int cls_ids[OBJ_NUMB_MAX_SIZE];
                    float confs[OBJ_NUMB_MAX_SIZE];
                    for (int i = 0; i < local_result.count; i++) {
                        cls_ids[i] = local_result.results[i].class_id;
                        confs[i] = local_result.results[i].prop;
                    }
                    hud_ipc_update_from_detections(
                        local_result.count, cls_ids, confs);
                }
            }
        }

        /* Release the frame back to the source module */
        if (src_type == RKNN_SRC_VI) {
            RK_MPI_VI_ReleaseChnFrame(dev_id, chn_id, &frame_info);
        } else {
            RK_MPI_VPSS_ReleaseChnFrame(dev_id, chn_id, &frame_info);
        }
    }

    ctx->thread_running = 0;
    printf("[INFO] RKNN inference thread exiting (processed %lu frames)\n",
           (unsigned long)ctx->total_frames);
    return NULL;
}

/* --------------------------------------------------------------------------
 * rknn_detect_start_thread
 * -------------------------------------------------------------------------- */

int rknn_detect_start_thread(rknn_detect_ctx_t *ctx,
                             int src_type, int dev_id, int chn_id) {
    if (!ctx || !ctx->initialized)
        return -1;

    if (ctx->thread_running) {
        printf("[WARN] Inference thread already running\n");
        return 0;
    }

    /* Prepare thread argument */
    infer_thread_arg_t *targ = (infer_thread_arg_t *)malloc(sizeof(infer_thread_arg_t));
    if (!targ) {
        printf("[ERROR] Failed to allocate thread argument\n");
        return -1;
    }
    targ->ctx      = ctx;
    targ->src_type = src_type;
    targ->dev_id   = dev_id;
    targ->chn_id   = chn_id;

    ctx->thread_should_stop = 0;
    ctx->thread_running = 1;

    int ret = pthread_create(&ctx->infer_thread, NULL,
                             inference_thread_func, targ);
    if (ret != 0) {
        printf("[ERROR] Failed to create inference thread: %s\n", strerror(ret));
        free(targ);
        ctx->thread_running = 0;
        return -1;
    }

    /* Detach is not needed -- we join in stop_thread */
    return 0;
}

/* --------------------------------------------------------------------------
 * rknn_detect_stop_thread
 * -------------------------------------------------------------------------- */

void rknn_detect_stop_thread(rknn_detect_ctx_t *ctx) {
    if (!ctx)
        return;

    if (ctx->thread_running) {
        printf("[INFO] Stopping RKNN inference thread...\n");
        ctx->thread_should_stop = 1;
        pthread_join(ctx->infer_thread, NULL);
        ctx->thread_running = 0;
        printf("[INFO] RKNN inference thread stopped\n");
    }
}

/* --------------------------------------------------------------------------
 * rknn_detect_get_result
 * -------------------------------------------------------------------------- */

int rknn_detect_get_result(rknn_detect_ctx_t *ctx,
                           detect_result_group_t *group) {
    if (!ctx || !group)
        return -1;

    pthread_mutex_lock(&ctx->result_mutex);
    memcpy(group, &ctx->det_result, sizeof(detect_result_group_t));
    pthread_mutex_unlock(&ctx->result_mutex);

    return 0;
}

/* --------------------------------------------------------------------------
 * rknn_detect_release
 * -------------------------------------------------------------------------- */

void rknn_detect_release(rknn_detect_ctx_t *ctx) {
    if (!ctx)
        return;

    /* Stop inference thread first */
    rknn_detect_stop_thread(ctx);

    rknn_context rknn_ctx = (rknn_context)ctx->rknn_ctx;

    /* Destroy output tensor memory */
    for (int i = 0; i < 3; i++) {
        if (ctx->output_mem[i]) {
            rknn_destroy_mem(rknn_ctx, (rknn_tensor_mem *)ctx->output_mem[i]);
            ctx->output_mem[i] = NULL;
        }
    }

    /* Destroy input tensor memory */
    if (ctx->input_mem) {
        rknn_destroy_mem(rknn_ctx, (rknn_tensor_mem *)ctx->input_mem);
        ctx->input_mem = NULL;
    }

    /* Destroy RKNN context */
    if (ctx->rknn_ctx) {
        rknn_destroy(rknn_ctx);
        ctx->rknn_ctx = 0;
    }

    /* Free RGB conversion buffers */
    if (ctx->rgb_buf) {
        free(ctx->rgb_buf);
        ctx->rgb_buf = NULL;
    }
    if (ctx->frame_rgb_buf) {
        free(ctx->frame_rgb_buf);
        ctx->frame_rgb_buf = NULL;
        ctx->frame_buf_w = 0;
        ctx->frame_buf_h = 0;
    }

    pthread_mutex_destroy(&ctx->result_mutex);

    ctx->initialized = 0;
    printf("[INFO] RKNN detector released\n");
}

/* --------------------------------------------------------------------------
 * rknn_detect_get_perf
 * -------------------------------------------------------------------------- */

void rknn_detect_get_perf(rknn_detect_ctx_t *ctx,
                          float *infer_ms, float *postproc_ms,
                          uint64_t *total_frames) {
    if (!ctx) return;

    if (infer_ms)
        *infer_ms = ctx->last_infer_ms;
    if (postproc_ms)
        *postproc_ms = ctx->last_postproc_ms;
    if (total_frames)
        *total_frames = ctx->total_frames;
}
