/*
 * rknn_detect.h
 *
 * RKNN NPU inference wrapper for YOLOv5n @ 320x320 INT8 on RV1106G3.
 *
 * Uses the RKNN C API with zero-copy memory (rknn_create_mem / rknn_set_io_mem)
 * which is the recommended approach on RV1106. The runtime library on this
 * platform is librknnmrt.so (mini runtime), not the full librknnrt.so.
 *
 * Thread-safety: The detector context is designed to run in a dedicated
 * inference thread. The public API is NOT thread-safe -- callers must
 * ensure serialized access or use one context per thread.
 *
 * Input: NV12 320x320 from VPSS CHN1 (zero-copy via RKMPI memory block)
 * Output: Detection results (bounding boxes, class labels, confidence)
 */

#ifndef _RKNN_DETECT_H_
#define _RKNN_DETECT_H_

#include <stdint.h>
#include <pthread.h>
#include "postprocess.h"

#ifdef __cplusplus
extern "C" {
#endif

/* --------------------------------------------------------------------------
 * Configuration
 * -------------------------------------------------------------------------- */

/* Default model path on the board filesystem */
#define RKNN_MODEL_PATH_DEFAULT "/root/model/yolov5n_320.rknn"

/* Inference thread frame grab timeout (milliseconds) */
#define RKNN_FRAME_TIMEOUT_MS   200

/* Maximum number of consecutive inference errors before logging a warning */
#define RKNN_MAX_CONSECUTIVE_ERRORS 10

/* --------------------------------------------------------------------------
 * Detector context (opaque to the caller, but exposed for stack allocation)
 * -------------------------------------------------------------------------- */

typedef struct _rknn_detect_ctx_t {
    /* RKNN context handle */
    uint32_t            rknn_ctx;       /* rknn_context (uint32_t on ARM32) */
    int                 initialized;

    /* I/O tensor attributes */
    uint32_t            n_input;
    uint32_t            n_output;

    /*
     * Tensor memory handles (RKNN zero-copy).
     * input_mem[0]  = model input  (320x320 RGB/NV12)
     * output_mem[0..2] = model outputs (3 detection heads)
     *
     * Using void* to avoid exposing rknn_api.h in this header.
     * Internally these are rknn_tensor_mem*.
     */
    void               *input_mem;
    void               *output_mem[3];

    /* Output quantization parameters (per-tensor affine) */
    int32_t             out_zps[3];
    float               out_scales[3];

    /* Model input dimensions (queried from model) */
    int                 model_width;
    int                 model_height;
    int                 model_channel;

    /* Input tensor stride info (for handling width padding) */
    uint32_t            input_w_stride;

    /* Detection results (double-buffered for thread safety) */
    detect_result_group_t det_result;
    pthread_mutex_t       result_mutex;

    /* Inference thread */
    pthread_t           infer_thread;
    volatile int        thread_running;
    volatile int        thread_should_stop;

    /* Performance counters */
    float               last_infer_ms;
    float               last_postproc_ms;
    uint64_t            total_frames;
    uint64_t            total_detections;

    /* NV12-to-RGB conversion buffer (320*320*3 bytes) */
    uint8_t            *rgb_buf;

} rknn_detect_ctx_t;

/* --------------------------------------------------------------------------
 * Public API
 * -------------------------------------------------------------------------- */

/*
 * rknn_detect_init - Initialize the RKNN detector.
 *
 * Loads the .rknn model file, queries I/O attributes, allocates zero-copy
 * tensor memory, and prepares the context for inference.
 *
 * @ctx:        Pointer to detector context (caller-allocated)
 * @model_path: Path to the .rknn model file (NULL uses default path)
 *
 * Returns 0 on success, negative on error.
 */
int rknn_detect_init(rknn_detect_ctx_t *ctx, const char *model_path);

/*
 * rknn_detect_run - Run inference on a single NV12 frame.
 *
 * Converts NV12 to RGB, copies into input tensor memory, runs inference,
 * and applies post-processing. Results are stored in ctx->det_result.
 *
 * @ctx:       Initialized detector context
 * @nv12_data: Pointer to NV12 frame data (320*320*1.5 bytes)
 * @width:     Frame width  (must be 320)
 * @height:    Frame height (must be 320)
 * @group:     Output detection results (caller-allocated)
 *
 * Returns 0 on success, negative on error.
 */
int rknn_detect_run(rknn_detect_ctx_t *ctx,
                    const uint8_t *nv12_data, int width, int height,
                    detect_result_group_t *group);

/*
 * rknn_detect_start_thread - Start the background inference thread.
 *
 * The thread continuously grabs frames from VPSS CHN1, runs inference,
 * and updates the latest detection results. Use rknn_detect_get_result()
 * to retrieve the most recent results.
 *
 * @ctx:         Initialized detector context
 * @vpss_grp:    VPSS group ID
 * @vpss_chn:    VPSS channel ID (typically CHN1 for NPU)
 *
 * Returns 0 on success, negative on error.
 */
int rknn_detect_start_thread(rknn_detect_ctx_t *ctx,
                             int vpss_grp, int vpss_chn);

/*
 * rknn_detect_stop_thread - Stop the background inference thread.
 *
 * Signals the thread to stop and waits for it to exit.
 * Safe to call even if thread was never started.
 *
 * @ctx: Detector context
 */
void rknn_detect_stop_thread(rknn_detect_ctx_t *ctx);

/*
 * rknn_detect_get_result - Get the latest detection results (thread-safe).
 *
 * Copies the most recent detection results into the caller's buffer.
 * This is safe to call from any thread while the inference thread is running.
 *
 * @ctx:   Detector context
 * @group: Output buffer for detection results
 *
 * Returns 0 on success, negative on error.
 */
int rknn_detect_get_result(rknn_detect_ctx_t *ctx,
                           detect_result_group_t *group);

/*
 * rknn_detect_release - Release all resources.
 *
 * Stops the inference thread (if running), destroys tensor memory,
 * and releases the RKNN context. The ctx struct can be reused after
 * calling rknn_detect_init() again.
 *
 * @ctx: Detector context
 */
void rknn_detect_release(rknn_detect_ctx_t *ctx);

/*
 * rknn_detect_get_perf - Get performance statistics.
 *
 * @ctx:          Detector context
 * @infer_ms:     Output: last inference time in ms (NULL to skip)
 * @postproc_ms:  Output: last post-processing time in ms (NULL to skip)
 * @total_frames: Output: total frames processed (NULL to skip)
 */
void rknn_detect_get_perf(rknn_detect_ctx_t *ctx,
                          float *infer_ms, float *postproc_ms,
                          uint64_t *total_frames);

#ifdef __cplusplus
}
#endif

#endif /* _RKNN_DETECT_H_ */
