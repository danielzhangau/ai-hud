/*
 * camera_display.c
 *
 * Camera + Display pipeline for Luckfox Pico Ultra (RV1106G3)
 * Pipeline: VI (SC3336 MIPI CSI) -> VPSS -> VO (DPI LCD 480x480)
 *
 * VI dual-channel configuration (official Luckfox pattern for RV1106):
 *   VI CHN0 (mainpath): 2304x1296 -> VPSS -> 480x480 -> VO (display, bind)
 *   VI CHN1 (selfpath): 640x640 -> NPU inference (user-mode GetChnFrame)
 *
 * Usage:
 *   ./ai-hud                              Display-only mode
 *   ./ai-hud --model /root/model/xxx.rknn Display + NPU inference
 *
 * Target: >= 25 FPS
 *
 * Build: see CMakeLists.txt (cross-compile with Luckfox SDK toolchain)
 */

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <signal.h>
#include <unistd.h>
#include <pthread.h>
#include <errno.h>
#include <fcntl.h>
#include <sys/mman.h>

#include "rk_mpi_vi.h"
#include "rk_mpi_vpss.h"
#include "rk_mpi_vo.h"
#include "rk_mpi_sys.h"
#include "rk_mpi_mb.h"

#include "isp_control.h"
#include "rknn_detect.h"
#include "overlay_draw.h"
#include "hud_ipc.h"

/* --------------------------------------------------------------------------
 * Constants
 * -------------------------------------------------------------------------- */

/* VI (Video Input) */
#define VI_PIPE_ID          0
#define VI_CHN_ID           0       /* Channel 0: mainpath -> VPSS -> VO  */
#define VI_CHN_NPU          1       /* Channel 1: selfpath -> NPU (user-mode) */
#define VI_WIDTH            2304    /* SC3336 native width  */
#define VI_HEIGHT           1296    /* SC3336 native height */

/*
 * VI CHN1 (NPU) output resolution.
 * The ISP selfpath crops from center and resizes to this square.
 * Using 640x640 to match model input size and avoid upscaling artifacts.
 */
#define VI_NPU_WIDTH        640
#define VI_NPU_HEIGHT       640

/* VPSS (Video Processing Sub-System) */
#define VPSS_GRP_ID         0
#define VPSS_CHN_DISPLAY    0       /* Channel 0: display output only   */

#define DISPLAY_WIDTH       480
#define DISPLAY_HEIGHT      480

/*
 * RV1106 VPSS limitation: bind mode GetChnFrame only yields initial
 * buffer frames (confirmed: 1 frame then NOBUF 0xa006800e).
 * Solution: VI dual-channel (official Luckfox pattern).
 *   CHN0 (mainpath) -> VPSS -> VO (display, bind)
 *   CHN1 (selfpath) -> GetChnFrame -> NPU inference (user-mode)
 */

/* VO (Video Output) */
#define VO_DEV_ID           0
#define VO_LAYER_ID         0
#define VO_CHN_ID           0
#define VO_SCREEN_WIDTH     480
#define VO_SCREEN_HEIGHT    480

/*
 * PiP (Picture-in-Picture) camera preview.
 * When enabled, the camera feed is displayed in a small window at the
 * bottom-right corner instead of full-screen.  The rest of the display
 * shows through the framebuffer (HUD from hud_live.py).
 *
 * Coordinates must match the PIP_* constants in hud_live.py.
 */
#define PIP_SIZE            120
#define PIP_MARGIN          10
#define PIP_X               (VO_SCREEN_WIDTH  - PIP_MARGIN - PIP_SIZE)  /* 350 */
#define PIP_Y               (VO_SCREEN_HEIGHT - PIP_MARGIN - PIP_SIZE)  /* 350 */

/* Framebuffer (for software PiP rendering -- bypasses VO/DRM) */
#define FB_DEV              "/dev/fb0"
#define FB_BPP              4                                  /* XRGB8888 */
#define FB_STRIDE           (VO_SCREEN_WIDTH * FB_BPP)         /* 1920 */
#define FB_SIZE             (FB_STRIDE * VO_SCREEN_HEIGHT)     /* 921600 */

/* Pipeline */
#define TARGET_FPS          25

/* --------------------------------------------------------------------------
 * Global state
 * -------------------------------------------------------------------------- */

static volatile int g_running = 1;

/* NPU detector context (initialized only when --model is specified) */
static rknn_detect_ctx_t g_detector;
static int               g_npu_enabled = 0;
static const char       *g_model_path  = NULL;

/* PiP mode: camera in small corner window, framebuffer HUD visible */
static int               g_pip_mode    = 0;

/* Software PiP framebuffer state */
static int               g_fb_fd       = -1;
static uint8_t          *g_fb_map      = NULL;

static void signal_handler(int sig) {
    (void)sig;
    /* Only set flag here -- printf is not async-signal-safe */
    g_running = 0;
}

static void install_signal_handlers(void) {
    struct sigaction sa;
    memset(&sa, 0, sizeof(sa));
    sa.sa_handler = signal_handler;
    sigemptyset(&sa.sa_mask);
    sa.sa_flags = 0;

    sigaction(SIGINT,  &sa, NULL);
    sigaction(SIGTERM, &sa, NULL);
}

/* --------------------------------------------------------------------------
 * ISP -- Image Signal Processor (must start before VI)
 *
 * Mandatory boot order per RKMPI spec:
 *   1) ISP Init -> ISP Run
 *   2) VI EnableDev -> VI EnableChn
 * Shutdown order:
 *   1) VI DisableChn -> VI DisableDev
 *   2) ISP Stop
 *
 * Uses isp_control module (direct RKAIQ uAPI2) instead of
 * SAMPLE_COMM_ISP wrapper, for runtime AE/DRC/NR tuning access.
 * -------------------------------------------------------------------------- */

#define ISP_CAM_ID          0
#define IQ_FILE_DIR         "/etc/iqfiles"

static int isp_init(void) {
    int ret;

    ret = isp_ctrl_init(ISP_CAM_ID, IQ_FILE_DIR);
    if (ret != 0) {
        printf("[ERROR] ISP init failed\n");
        return ret;
    }

    /* Apply dashcam-optimized AE/DRC/NR defaults */
    isp_ctrl_apply_defaults();

    return 0;
}

static void isp_deinit(void) {
    isp_ctrl_deinit();
}

/* --------------------------------------------------------------------------
 * VI -- Video Input (camera capture via ISP)
 *
 * Follows the official LuckfoxTECH RKMPI example pattern:
 *   - All-zero VI_DEV_ATTR_S (defaults handled by driver)
 *   - Check-before-configure to avoid double-init
 *   - Bind dev to pipe explicitly
 *   - DMA-BUF memory type for VI channel ISP option
 * -------------------------------------------------------------------------- */

static int vi_init(void) {
    int ret;
    int dev_id  = VI_PIPE_ID;
    int pipe_id = VI_PIPE_ID;

    /* ---- Configure VI device (use all-zero defaults, driver auto-fills) ---- */
    VI_DEV_ATTR_S dev_attr;
    memset(&dev_attr, 0, sizeof(dev_attr));

    ret = RK_MPI_VI_GetDevAttr(dev_id, &dev_attr);
    if (ret == RK_ERR_VI_NOT_CONFIG) {
        ret = RK_MPI_VI_SetDevAttr(dev_id, &dev_attr);
        if (ret != 0) {
            printf("[ERROR] RK_MPI_VI_SetDevAttr failed: 0x%x\n", ret);
            return ret;
        }
    }

    /* ---- Enable device if not already enabled ---- */
    ret = RK_MPI_VI_GetDevIsEnable(dev_id);
    if (ret != 0) {
        ret = RK_MPI_VI_EnableDev(dev_id);
        if (ret != 0) {
            printf("[ERROR] RK_MPI_VI_EnableDev failed: 0x%x\n", ret);
            return ret;
        }

        /* ---- Bind device to pipe ---- */
        VI_DEV_BIND_PIPE_S bind_pipe;
        memset(&bind_pipe, 0, sizeof(bind_pipe));
        bind_pipe.u32Num     = 1;
        bind_pipe.PipeId[0]  = pipe_id;

        ret = RK_MPI_VI_SetDevBindPipe(dev_id, &bind_pipe);
        if (ret != 0) {
            printf("[ERROR] RK_MPI_VI_SetDevBindPipe failed: 0x%x\n", ret);
            return ret;
        }
    }

    /* ---- Configure VI channel 0 (mainpath -> VPSS -> VO) ---- */
    VI_CHN_ATTR_S chn_attr;
    memset(&chn_attr, 0, sizeof(chn_attr));
    chn_attr.stSize.u32Width       = VI_WIDTH;
    chn_attr.stSize.u32Height      = VI_HEIGHT;
    chn_attr.enPixelFormat         = RK_FMT_YUV420SP;   /* NV12 */
    chn_attr.u32Depth              = 0;                  /* Bound to VPSS, no user GetChnFrame */
    chn_attr.enCompressMode        = COMPRESS_MODE_NONE;
    chn_attr.stIspOpt.u32BufCount  = 4;                  /* Increased for dual-channel */
    chn_attr.stIspOpt.enMemoryType = VI_V4L2_MEMORY_TYPE_DMABUF;

    ret = RK_MPI_VI_SetChnAttr(pipe_id, VI_CHN_ID, &chn_attr);
    if (ret != 0) {
        printf("[ERROR] RK_MPI_VI_SetChnAttr(CHN0) failed: 0x%x\n", ret);
        return ret;
    }

    ret = RK_MPI_VI_EnableChn(pipe_id, VI_CHN_ID);
    if (ret != 0) {
        printf("[ERROR] RK_MPI_VI_EnableChn(CHN0) failed: 0x%x\n", ret);
        return ret;
    }

    printf("[INFO] VI CHN0 (mainpath): pipe=%d, %dx%d NV12\n",
           pipe_id, VI_WIDTH, VI_HEIGHT);

    /* ---- Configure VI channel 1 (selfpath -> NPU inference) ---- */
    if (g_model_path) {
        VI_CHN_ATTR_S npu_chn_attr;
        memset(&npu_chn_attr, 0, sizeof(npu_chn_attr));
        npu_chn_attr.stSize.u32Width       = VI_NPU_WIDTH;
        npu_chn_attr.stSize.u32Height      = VI_NPU_HEIGHT;
        npu_chn_attr.enPixelFormat         = RK_FMT_YUV420SP;   /* NV12 */
        npu_chn_attr.u32Depth              = 2;                  /* User-mode GetChnFrame */
        npu_chn_attr.enCompressMode        = COMPRESS_MODE_NONE;
        npu_chn_attr.stIspOpt.u32BufCount  = 4;
        npu_chn_attr.stIspOpt.enMemoryType = VI_V4L2_MEMORY_TYPE_DMABUF;

        ret = RK_MPI_VI_SetChnAttr(pipe_id, VI_CHN_NPU, &npu_chn_attr);
        if (ret != 0) {
            printf("[WARN] RK_MPI_VI_SetChnAttr(CHN1) failed: 0x%x\n", ret);
            /* Non-fatal: NPU will be disabled */
        } else {
            ret = RK_MPI_VI_EnableChn(pipe_id, VI_CHN_NPU);
            if (ret != 0) {
                printf("[WARN] RK_MPI_VI_EnableChn(CHN1) failed: 0x%x\n", ret);
            } else {
                printf("[INFO] VI CHN1 (selfpath): pipe=%d, %dx%d NV12 (NPU)\n",
                       pipe_id, VI_NPU_WIDTH, VI_NPU_HEIGHT);
            }
        }
    }

    return 0;
}

static void vi_deinit(void) {
    RK_MPI_VI_DisableChn(VI_PIPE_ID, VI_CHN_NPU);   /* CHN1: NPU (safe even if not enabled) */
    RK_MPI_VI_DisableChn(VI_PIPE_ID, VI_CHN_ID);     /* CHN0: mainpath */
    RK_MPI_VI_DisableDev(VI_PIPE_ID);
    printf("[INFO] VI deinitialized\n");
}

/* --------------------------------------------------------------------------
 * VPSS -- Video Processing Sub-System (resize + format conversion)
 * -------------------------------------------------------------------------- */

static int vpss_init(void) {
    int ret;

    /* ---- VPSS Group ---- */
    VPSS_GRP_ATTR_S grp_attr;
    memset(&grp_attr, 0, sizeof(grp_attr));
    grp_attr.u32MaxW       = VI_WIDTH;
    grp_attr.u32MaxH       = VI_HEIGHT;
    grp_attr.enPixelFormat = RK_FMT_YUV420SP;      /* Input format from VI */
    grp_attr.enCompressMode = COMPRESS_MODE_NONE;
    grp_attr.stFrameRate.s32SrcFrameRate  = -1;     /* Follow source */
    grp_attr.stFrameRate.s32DstFrameRate  = -1;

    ret = RK_MPI_VPSS_CreateGrp(VPSS_GRP_ID, &grp_attr);
    if (ret != 0) {
        printf("[ERROR] RK_MPI_VPSS_CreateGrp failed: 0x%x\n", ret);
        return ret;
    }

    /* ---- Channel 0: 480x480 NV12 for display ---- */
    VPSS_CHN_ATTR_S chn0_attr;
    memset(&chn0_attr, 0, sizeof(chn0_attr));
    chn0_attr.enChnMode                    = VPSS_CHN_MODE_AUTO;
    chn0_attr.u32Width                     = DISPLAY_WIDTH;
    chn0_attr.u32Height                    = DISPLAY_HEIGHT;
    chn0_attr.enPixelFormat                = RK_FMT_YUV420SP;  /* NV12 */
    chn0_attr.enCompressMode               = COMPRESS_MODE_NONE;
    chn0_attr.stFrameRate.s32SrcFrameRate  = -1;
    chn0_attr.stFrameRate.s32DstFrameRate  = -1;
    if (g_pip_mode) {
        /* PiP: user-mode frames for software framebuffer rendering */
        chn0_attr.u32Depth       = 2;
        chn0_attr.u32FrameBufCnt = 4;
    } else {
        /* Full-screen: bound to VO, no user GetChnFrame */
        chn0_attr.u32Depth       = 0;
        chn0_attr.u32FrameBufCnt = 3;
    }

    ret = RK_MPI_VPSS_SetChnAttr(VPSS_GRP_ID, VPSS_CHN_DISPLAY, &chn0_attr);
    if (ret != 0) {
        printf("[ERROR] RK_MPI_VPSS_SetChnAttr(CHN0) failed: 0x%x\n", ret);
        return ret;
    }

    ret = RK_MPI_VPSS_EnableChn(VPSS_GRP_ID, VPSS_CHN_DISPLAY);
    if (ret != 0) {
        printf("[ERROR] RK_MPI_VPSS_EnableChn(CHN0) failed: 0x%x\n", ret);
        return ret;
    }

    /* ---- Start VPSS group ---- */
    ret = RK_MPI_VPSS_StartGrp(VPSS_GRP_ID);
    if (ret != 0) {
        printf("[ERROR] RK_MPI_VPSS_StartGrp failed: 0x%x\n", ret);
        return ret;
    }

    printf("[INFO] VPSS initialized: grp=%d, CHN0 %dx%d NV12 (display+NPU)\n",
           VPSS_GRP_ID, DISPLAY_WIDTH, DISPLAY_HEIGHT);
    return 0;
}

static void vpss_deinit(void) {
    /* Disable channel before stopping group (RKMPI requirement) */
    RK_MPI_VPSS_DisableChn(VPSS_GRP_ID, VPSS_CHN_DISPLAY);
    RK_MPI_VPSS_StopGrp(VPSS_GRP_ID);
    RK_MPI_VPSS_DestroyGrp(VPSS_GRP_ID);
    printf("[INFO] VPSS deinitialized\n");
}

/* --------------------------------------------------------------------------
 * Software PiP -- NV12-to-XRGB conversion + framebuffer direct write
 *
 * In PiP mode we bypass VO entirely (VO Enable takes over the DRM CRTC
 * and disables the framebuffer primary plane).  Instead, VPSS outputs
 * 480x480 NV12 in user-mode, and this thread point-samples down to
 * 120x120 XRGB8888, writing directly into the mmap'd framebuffer.
 * -------------------------------------------------------------------------- */

static inline int clamp8(int v) {
    return v < 0 ? 0 : (v > 255 ? 255 : v);
}

/*
 * NV12 480x480 -> XRGB fullscreen (480x480) for CAM display mode.
 * Reserves a top-left corner for the Python-rendered menu icon.
 * BT.601 full-range conversion, integer fixed-point arithmetic.
 */

/* Top-left pixel region reserved for Python menu icon overlay.
 * Python `_draw_menu_icon()` draws a hamburger menu here (three horizontal
 * lines within ~30px).  Use 40px for padding.  C never overwrites this
 * region; Python `flush_rect(0, 0, 40, 40)` manages it independently. */
#define GEAR_RESERVE_SIZE   40

static void nv12_480_to_fullscreen_xrgb(const uint8_t *nv12, uint8_t *fb) {
    const int w = DISPLAY_WIDTH;
    const int h = DISPLAY_HEIGHT;
    const uint8_t *y_plane  = nv12;
    const uint8_t *uv_plane = nv12 + w * h;

    for (int row = 0; row < h; row++) {
        /* Skip reserved gear icon region (top-left corner) */
        int col_start = (row < GEAR_RESERVE_SIZE) ? GEAR_RESERVE_SIZE : 0;
        for (int col = col_start; col < w; col++) {
            int y_val  = y_plane[row * w + col];
            int uv_off = (row / 2) * w + (col & ~1);
            int u = uv_plane[uv_off]     - 128;
            int v = uv_plane[uv_off + 1] - 128;

            int r = clamp8(y_val + ((359 * v) >> 8));
            int g = clamp8(y_val - ((88 * u + 183 * v) >> 8));
            int b = clamp8(y_val + ((454 * u) >> 8));

            int off = (row * w + col) * FB_BPP;
            fb[off]     = (uint8_t)b;
            fb[off + 1] = (uint8_t)g;
            fb[off + 2] = (uint8_t)r;
            fb[off + 3] = 0xFF;
        }
    }
}

/*
 * Display-mode IPC file written by Python (hud_live.py).
 * File contains "cam\n" for camera mode; absent or "hud\n" for HUD mode.
 * When settings overlay is active, /tmp/ai_hud_pip_hide exists and
 * camera rendering is paused regardless of display mode.
 */
#define DISPLAY_MODE_IPC   HUD_IPC_DISPLAY_MODE
#define PIP_HIDE_IPC       "/tmp/ai_hud_pip_hide"

static int read_display_mode_cam(void) {
    /* Returns 1 if CAM mode requested, 0 for HUD (default). */
    FILE *f = fopen(DISPLAY_MODE_IPC, "r");
    if (!f) return 0;
    char buf[8] = {0};
    fread(buf, 1, sizeof(buf) - 1, f);
    fclose(f);
    return (buf[0] == 'c');  /* "cam" */
}

static void *pip_render_thread(void *arg) {
    (void)arg;

    /*
     * Wait for HUD (hud_live.py) to signal readiness before rendering.
     * Without this, camera frames overwrite the splash screen before the
     * Python HUD has started.  Timeout after 15s (fallback: start anyway).
     */
    printf("[INFO] PiP: waiting for HUD ready signal...\n");
    for (int i = 0; i < 150 && g_running; i++) {
        if (access("/tmp/ai_hud_ready", F_OK) == 0)
            break;
        usleep(100 * 1000);
    }
    if (!g_running)
        return NULL;
    printf("[INFO] PiP: HUD ready, camera render thread active\n");

    /* Open and mmap framebuffer */
    g_fb_fd = open(FB_DEV, O_RDWR);
    if (g_fb_fd < 0) {
        printf("[ERROR] PiP: failed to open %s: %s\n", FB_DEV, strerror(errno));
        return NULL;
    }

    g_fb_map = mmap(NULL, FB_SIZE, PROT_READ | PROT_WRITE, MAP_SHARED,
                    g_fb_fd, 0);
    if (g_fb_map == MAP_FAILED) {
        printf("[ERROR] PiP: failed to mmap framebuffer: %s\n", strerror(errno));
        g_fb_map = NULL;
        close(g_fb_fd);
        g_fb_fd = -1;
        return NULL;
    }

    printf("[INFO] PiP render: fb mapped (%dx%d XRGB8888, stride=%d)\n",
           VO_SCREEN_WIDTH, VO_SCREEN_HEIGHT, FB_STRIDE);

    VIDEO_FRAME_INFO_S frame;
    int frame_count = 0;
    int mode_check_counter = 0;
    int cam_mode = 0;
    int pip_hidden = 0;

    while (g_running) {
        int ret = RK_MPI_VPSS_GetChnFrame(VPSS_GRP_ID, VPSS_CHN_DISPLAY,
                                           &frame, 100);
        if (ret != 0)
            continue;

        /* Check display mode IPC every 15 frames (~0.6s at 25fps) */
        if (++mode_check_counter >= 15) {
            mode_check_counter = 0;
            cam_mode = read_display_mode_cam();
            pip_hidden = (access(PIP_HIDE_IPC, F_OK) == 0);
        }

        void *vaddr = RK_MPI_MB_Handle2VirAddr(frame.stVFrame.pMbBlk);
        if (vaddr && cam_mode && !pip_hidden) {
            /* Validate frame dimensions match expectations before pixel access.
             * Prevents buffer overflow if VPSS outputs unexpected resolution. */
            if (frame.stVFrame.u32Width  == DISPLAY_WIDTH &&
                frame.stVFrame.u32Height == DISPLAY_HEIGHT &&
                g_fb_map != NULL) {
                nv12_480_to_fullscreen_xrgb((const uint8_t *)vaddr, g_fb_map);

                /* Overlay NPU detection bounding boxes + labels */
                if (g_npu_enabled) {
                    detect_result_group_t det_snap;
                    if (rknn_detect_get_result(&g_detector, &det_snap) == 0
                        && det_snap.count > 0) {
                        overlay_draw_detections(g_fb_map,
                                                DISPLAY_WIDTH, DISPLAY_HEIGHT,
                                                &det_snap);
                    }
                }

                frame_count++;
            }
        }
        /* HUD mode: no camera rendering to framebuffer */

        RK_MPI_VPSS_ReleaseChnFrame(VPSS_GRP_ID, VPSS_CHN_DISPLAY, &frame);
    }

    printf("[INFO] PiP render exiting (%d frames rendered)\n", frame_count);

    if (g_fb_map) {
        munmap(g_fb_map, FB_SIZE);
        g_fb_map = NULL;
    }
    if (g_fb_fd >= 0) {
        close(g_fb_fd);
        g_fb_fd = -1;
    }
    return NULL;
}

/* --------------------------------------------------------------------------
 * VO -- Video Output (DPI LCD panel)
 * -------------------------------------------------------------------------- */

static int vo_init(void) {
    int ret;

    /*
     * VO init order from official demo (simple_vi_get_frame_send_vo_rv1106):
     *   1) BindLayer (GRAPHIC mode -- VIDEO mode triggers GPU compositor crash)
     *   2) SetPubAttr -> Enable
     *   3) SetLayerAttr -> SetLayerSpliceMode(RGA) -> EnableLayer
     *   4) SetChnAttr -> EnableChn
     */

    /* ---- Bind layer to device FIRST (must precede everything else) ---- */
    ret = RK_MPI_VO_BindLayer(VO_LAYER_ID, VO_DEV_ID, VO_LAYER_MODE_GRAPHIC);
    if (ret != 0) {
        printf("[ERROR] RK_MPI_VO_BindLayer failed: 0x%x\n", ret);
        return ret;
    }

    /* ---- VO device (display device) ---- */
    VO_PUB_ATTR_S pub_attr;
    memset(&pub_attr, 0, sizeof(pub_attr));
    pub_attr.enIntfType  = VO_INTF_DEFAULT;          /* DPI RGB interface */
    pub_attr.enIntfSync  = VO_OUTPUT_DEFAULT;

    ret = RK_MPI_VO_SetPubAttr(VO_DEV_ID, &pub_attr);
    if (ret != 0) {
        printf("[ERROR] RK_MPI_VO_SetPubAttr failed: 0x%x\n", ret);
        return ret;
    }

    ret = RK_MPI_VO_Enable(VO_DEV_ID);
    if (ret != 0) {
        printf("[ERROR] RK_MPI_VO_Enable failed: 0x%x\n", ret);
        return ret;
    }

    /* ---- VO layer (GRAPHIC mode + RGA splice, matching official demo) ---- */
    /* Note: vo_init() is only called when g_pip_mode == 0 (see main()),
     * so this is always the full-screen path. */
    VO_VIDEO_LAYER_ATTR_S layer_attr;
    memset(&layer_attr, 0, sizeof(layer_attr));

    layer_attr.stDispRect.s32X      = 0;
    layer_attr.stDispRect.s32Y      = 0;
    layer_attr.stDispRect.u32Width  = VO_SCREEN_WIDTH;
    layer_attr.stDispRect.u32Height = VO_SCREEN_HEIGHT;
    layer_attr.stImageSize.u32Width  = VO_SCREEN_WIDTH;
    layer_attr.stImageSize.u32Height = VO_SCREEN_HEIGHT;
    layer_attr.enPixFormat           = RK_FMT_RGB888;
    layer_attr.enCompressMode        = COMPRESS_AFBC_16x16;
    layer_attr.u32DispFrmRt          = TARGET_FPS;

    ret = RK_MPI_VO_SetLayerAttr(VO_LAYER_ID, &layer_attr);
    if (ret != 0) {
        printf("[ERROR] RK_MPI_VO_SetLayerAttr failed: 0x%x\n", ret);
        return ret;
    }

    /* Use RGA for compositing (RV1106 has no GPU -- avoids libgraphic_lsf.so assertion) */
    RK_MPI_VO_SetLayerSpliceMode(VO_LAYER_ID, VO_SPLICE_MODE_RGA);

    ret = RK_MPI_VO_EnableLayer(VO_LAYER_ID);
    if (ret != 0) {
        printf("[ERROR] RK_MPI_VO_EnableLayer failed: 0x%x\n", ret);
        return ret;
    }

    /* ---- VO channel (fills the layer area) ---- */
    VO_CHN_ATTR_S chn_attr;
    memset(&chn_attr, 0, sizeof(chn_attr));
    /* Channel rect fills the layer's virtual canvas (stImageSize = 480x480). */
    chn_attr.stRect.s32X      = 0;
    chn_attr.stRect.s32Y      = 0;
    chn_attr.stRect.u32Width  = DISPLAY_WIDTH;
    chn_attr.stRect.u32Height = DISPLAY_HEIGHT;
    chn_attr.u32Priority      = 0;
    chn_attr.u32FgAlpha       = 255;
    chn_attr.u32BgAlpha       = 0;

    ret = RK_MPI_VO_SetChnAttr(VO_LAYER_ID, VO_CHN_ID, &chn_attr);
    if (ret != 0) {
        printf("[ERROR] RK_MPI_VO_SetChnAttr failed: 0x%x\n", ret);
        return ret;
    }

    ret = RK_MPI_VO_EnableChn(VO_LAYER_ID, VO_CHN_ID);
    if (ret != 0) {
        printf("[ERROR] RK_MPI_VO_EnableChn failed: 0x%x\n", ret);
        return ret;
    }

    printf("[INFO] VO initialized: dev=%d, layer=%d, chn=%d, %dx%d @ %dfps\n",
           VO_DEV_ID, VO_LAYER_ID, VO_CHN_ID,
           VO_SCREEN_WIDTH, VO_SCREEN_HEIGHT, TARGET_FPS);
    return 0;
}

static void vo_deinit(void) {
    RK_MPI_VO_DisableChn(VO_LAYER_ID, VO_CHN_ID);
    RK_MPI_VO_DisableLayer(VO_LAYER_ID);
    RK_MPI_VO_UnBindLayer(VO_LAYER_ID, VO_DEV_ID);
    RK_MPI_VO_Disable(VO_DEV_ID);
    printf("[INFO] VO deinitialized\n");
}

/* --------------------------------------------------------------------------
 * Module binding helpers
 * -------------------------------------------------------------------------- */

static MPP_CHN_S g_vi_chn;
static MPP_CHN_S g_vpss_grp;
static MPP_CHN_S g_vpss_chn_display;
static MPP_CHN_S g_vo_chn;

static void setup_bind_params(void) {
    /* VI output */
    g_vi_chn.enModId   = RK_ID_VI;
    g_vi_chn.s32DevId  = VI_PIPE_ID;
    g_vi_chn.s32ChnId  = VI_CHN_ID;

    /* VPSS group input */
    g_vpss_grp.enModId  = RK_ID_VPSS;
    g_vpss_grp.s32DevId = VPSS_GRP_ID;
    g_vpss_grp.s32ChnId = 0;   /* Group input, chn=0 */

    /* VPSS channel 0 output (display) */
    g_vpss_chn_display.enModId  = RK_ID_VPSS;
    g_vpss_chn_display.s32DevId = VPSS_GRP_ID;
    g_vpss_chn_display.s32ChnId = VPSS_CHN_DISPLAY;

    /* VO channel input */
    g_vo_chn.enModId  = RK_ID_VO;
    g_vo_chn.s32DevId = VO_LAYER_ID;
    g_vo_chn.s32ChnId = VO_CHN_ID;
}

static int bind_pipeline(void) {
    int ret;

    /* VI -> VPSS */
    ret = RK_MPI_SYS_Bind(&g_vi_chn, &g_vpss_grp);
    if (ret != 0) {
        printf("[ERROR] Bind VI -> VPSS failed: 0x%x\n", ret);
        return ret;
    }
    printf("[INFO] Bound VI(pipe=%d,chn=%d) -> VPSS(grp=%d)\n",
           VI_PIPE_ID, VI_CHN_ID, VPSS_GRP_ID);

    /* VPSS CHN0 -> VO (skip in PiP mode: software rendering via framebuffer) */
    if (!g_pip_mode) {
        ret = RK_MPI_SYS_Bind(&g_vpss_chn_display, &g_vo_chn);
        if (ret != 0) {
            printf("[ERROR] Bind VPSS -> VO failed: 0x%x\n", ret);
            return ret;
        }
        printf("[INFO] Bound VPSS(grp=%d,chn=%d) -> VO(layer=%d,chn=%d)\n",
               VPSS_GRP_ID, VPSS_CHN_DISPLAY, VO_LAYER_ID, VO_CHN_ID);
    }

    return 0;
}

static void unbind_pipeline(void) {
    if (!g_pip_mode)
        RK_MPI_SYS_UnBind(&g_vpss_chn_display, &g_vo_chn);
    RK_MPI_SYS_UnBind(&g_vi_chn, &g_vpss_grp);
    printf("[INFO] Pipeline unbound\n");
}

/* --------------------------------------------------------------------------
 * FPS monitoring (optional debug output)
 * -------------------------------------------------------------------------- */

static void *fps_monitor_thread(void *arg) {
    (void)arg;

    printf("[INFO] FPS monitor thread started\n");

    while (g_running) {
        sleep(5);
        if (!g_running)
            break;

        /* Poll ISP IPC for night mode / config changes */
        isp_ctrl_poll_ipc();

        if (g_npu_enabled) {
            float infer_ms, postproc_ms;
            uint64_t total_frames;
            rknn_detect_get_perf(&g_detector, &infer_ms, &postproc_ms,
                                 &total_frames);
            printf("[INFO] Pipeline running | NPU: infer=%.1fms post=%.1fms "
                   "frames=%lu\n",
                   infer_ms, postproc_ms, (unsigned long)total_frames);
        } else {
            printf("[INFO] Pipeline running... (target: %d FPS)\n", TARGET_FPS);
        }
    }

    printf("[INFO] FPS monitor thread exiting\n");
    return NULL;
}

/* --------------------------------------------------------------------------
 * Main
 * -------------------------------------------------------------------------- */

static void print_usage(const char *prog) {
    printf("Usage: %s [options]\n", prog);
    printf("Options:\n");
    printf("  -m, --model <path>  RKNN model path (enables NPU inference)\n");
    printf("      --fullcam       Full-screen camera (disable PiP mode)\n");
    printf("  -h, --help          Show this help\n");
    printf("\n");
    printf("Without --model: full-screen camera preview.\n");
    printf("With --model:    PiP camera (%dx%d) + NPU inference.\n",
           PIP_SIZE, PIP_SIZE);
    printf("With --fullcam:  force full-screen camera (even with --model).\n");
}

int main(int argc, char *argv[]) {
    int ret;
    pthread_t fps_tid;

    /* ---- Parse command-line arguments ---- */
    int force_fullcam = 0;
    for (int i = 1; i < argc; i++) {
        if ((strcmp(argv[i], "-m") == 0 || strcmp(argv[i], "--model") == 0)
            && i + 1 < argc) {
            g_model_path = argv[++i];
        } else if (strcmp(argv[i], "--fullcam") == 0) {
            force_fullcam = 1;
        } else if (strcmp(argv[i], "-h") == 0 ||
                   strcmp(argv[i], "--help") == 0) {
            print_usage(argv[0]);
            return 0;
        } else {
            printf("[WARN] Unknown argument: %s\n", argv[i]);
        }
    }

    /* PiP mode: enabled by default when model is specified */
    g_pip_mode = (g_model_path != NULL) && !force_fullcam;

    printf("============================================\n");
    printf("  AI-HUD Camera Display Pipeline\n");
    printf("  Platform: Luckfox Pico Ultra (RV1106G3)\n");
    printf("  Camera:   SC3336 MIPI CSI\n");
    printf("  Display:  DPI LCD %dx%d\n", VO_SCREEN_WIDTH, VO_SCREEN_HEIGHT);
    printf("  Target:   >= %d FPS\n", TARGET_FPS);
    if (g_model_path)
        printf("  Model:    %s\n", g_model_path);
    else
        printf("  NPU:      disabled (use --model to enable)\n");
    printf("  Display:  %s\n", g_pip_mode ? "PiP (120x120 bottom-right)" : "full-screen camera");
    printf("============================================\n");

    /* Signal handlers for graceful shutdown */
    install_signal_handlers();

    /* ---- Step 1: ISP must start before VI (RKMPI requirement) ---- */
    ret = isp_init();
    if (ret != 0) {
        printf("[ERROR] ISP init failed, aborting\n");
        return -1;
    }

    /* ---- Step 2: Initialize MPI system ---- */
    ret = RK_MPI_SYS_Init();
    if (ret != 0) {
        printf("[ERROR] RK_MPI_SYS_Init failed: 0x%x\n", ret);
        goto cleanup_isp;
    }
    printf("[INFO] MPI system initialized\n");

    /* ---- Step 3: Initialize modules ---- */
    ret = vi_init();
    if (ret != 0) {
        printf("[ERROR] VI init failed, aborting\n");
        goto cleanup_sys;
    }

    ret = vpss_init();
    if (ret != 0) {
        printf("[ERROR] VPSS init failed, aborting\n");
        goto cleanup_vi;
    }

    /* VO is only used in full-screen mode.  In PiP mode, the software
     * PiP thread renders camera frames directly to the framebuffer,
     * avoiding the VO/DRM overlay plane that would disable /dev/fb0. */
    if (!g_pip_mode) {
        ret = vo_init();
        if (ret != 0) {
            printf("[ERROR] VO init failed, aborting\n");
            goto cleanup_vpss;
        }
    }

    /* ---- Step 3: Bind pipeline ---- */
    setup_bind_params();
    ret = bind_pipeline();
    if (ret != 0) {
        printf("[ERROR] Pipeline bind failed, aborting\n");
        goto cleanup_vo;
    }

    if (g_pip_mode)
        printf("[INFO] Pipeline started: VI -> VPSS -> software PiP\n");
    else
        printf("[INFO] Pipeline started: VI -> VPSS -> VO\n");

    /* ---- Step 4: Initialize NPU inference (optional) ---- */
    if (g_model_path) {
        ret = rknn_detect_init(&g_detector, g_model_path);
        if (ret != 0) {
            printf("[WARN] RKNN init failed, continuing without NPU\n");
        } else {
            ret = rknn_detect_start_thread(&g_detector,
                                           RKNN_SRC_VI, VI_PIPE_ID, VI_CHN_NPU);
            if (ret != 0) {
                printf("[WARN] RKNN thread start failed\n");
                rknn_detect_release(&g_detector);
            } else {
                g_npu_enabled = 1;
                printf("[INFO] NPU inference thread started on VI CHN%d\n",
                       VI_CHN_NPU);
            }
        }
    }

    printf("[INFO] Press Ctrl+C to stop\n");

    /* ---- Step 4b: Start PiP render thread (reads VPSS, writes framebuffer) ---- */
    pthread_t pip_tid;
    int pip_thread_created = 0;
    if (g_pip_mode) {
        ret = pthread_create(&pip_tid, NULL, pip_render_thread, NULL);
        if (ret != 0) {
            printf("[WARN] Failed to create PiP render thread: %s\n",
                   strerror(ret));
        } else {
            pip_thread_created = 1;
            printf("[INFO] PiP render thread started\n");
        }
    }

    /* ---- Step 5: Start FPS monitor thread ---- */
    int fps_thread_created = 0;
    ret = pthread_create(&fps_tid, NULL, fps_monitor_thread, NULL);
    if (ret != 0) {
        printf("[WARN] Failed to create FPS monitor thread: %s\n", strerror(ret));
        /* Non-fatal, continue without monitoring */
    } else {
        fps_thread_created = 1;
    }

    /* ---- Step 6: Main loop -- wait for exit signal ---- */
    /*
     * In bind mode, frames flow automatically through the hardware pipeline:
     *   VI captures -> VPSS scales -> VO displays
     * No manual frame get/release is needed for the display path.
     *
     * When --model is specified, the NPU inference thread reads from
     * VI CHN1 (selfpath) via GetChnFrame(), independent of the display
     * pipeline. Frames are resized in software to model input size.
     */
    while (g_running) {
        usleep(100 * 1000);  /* 100ms idle poll */
    }

    printf("[INFO] Shutting down pipeline...\n");

    /* ---- Step 7: Cleanup (reverse order) ---- */

    /* Stop NPU inference thread first (it reads from VI CHN1) */
    if (g_npu_enabled) {
        rknn_detect_release(&g_detector);
        g_npu_enabled = 0;
        printf("[INFO] NPU released\n");
    }

    if (pip_thread_created)
        pthread_join(pip_tid, NULL);

    if (fps_thread_created)
        pthread_join(fps_tid, NULL);

    unbind_pipeline();

cleanup_vo:
    if (!g_pip_mode)
        vo_deinit();

cleanup_vpss:
    vpss_deinit();

cleanup_vi:
    vi_deinit();

cleanup_sys:
    RK_MPI_SYS_Exit();
    printf("[INFO] MPI system exited\n");

cleanup_isp:
    isp_deinit();

    printf("[INFO] Shutdown complete\n");
    return 0;
}
