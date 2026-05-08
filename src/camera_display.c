/*
 * camera_display.c
 *
 * Camera + Display pipeline for Luckfox Pico Ultra (RV1106G3)
 * Pipeline: VI (SC3336 MIPI CSI) -> VPSS -> VO (DPI LCD 480x480)
 *
 * VPSS dual-channel configuration:
 *   Channel 0: 480x480 NV12 -> VO display output
 *   Channel 1: 320x320 NV12 -> Reserved for NPU inference
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

#include "rk_mpi_vi.h"
#include "rk_mpi_vpss.h"
#include "rk_mpi_vo.h"
#include "rk_mpi_sys.h"
#include "rk_mpi_mb.h"
#include "sample_comm_isp.h"

/* --------------------------------------------------------------------------
 * Constants
 * -------------------------------------------------------------------------- */

/* VI (Video Input) */
#define VI_PIPE_ID          0
#define VI_CHN_ID           0
#define VI_WIDTH            2304    /* SC3336 native width  */
#define VI_HEIGHT           1296    /* SC3336 native height */

/* VPSS (Video Processing Sub-System) */
#define VPSS_GRP_ID         0
#define VPSS_CHN_DISPLAY    0       /* Channel 0: display output   */
#define VPSS_CHN_NPU        1       /* Channel 1: NPU input (reserved) */

#define DISPLAY_WIDTH       480
#define DISPLAY_HEIGHT      480

#define NPU_WIDTH           320
#define NPU_HEIGHT          320

/* VO (Video Output) */
#define VO_DEV_ID           0
#define VO_LAYER_ID         0
#define VO_CHN_ID           0
#define VO_SCREEN_WIDTH     480
#define VO_SCREEN_HEIGHT    480

/* Pipeline */
#define TARGET_FPS          25

/* --------------------------------------------------------------------------
 * Global state
 * -------------------------------------------------------------------------- */

static volatile int g_running = 1;

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
 * -------------------------------------------------------------------------- */

#define ISP_CAM_ID          0
#define IQ_FILE_DIR         "/etc/iqfiles"

static int isp_init(void) {
    int ret;

    ret = SAMPLE_COMM_ISP_Init(ISP_CAM_ID, RK_AIQ_WORKING_MODE_NORMAL,
                               RK_FALSE, IQ_FILE_DIR);
    if (ret != 0) {
        printf("[ERROR] SAMPLE_COMM_ISP_Init failed: %d\n", ret);
        return ret;
    }

    ret = SAMPLE_COMM_ISP_Run(ISP_CAM_ID);
    if (ret != 0) {
        printf("[ERROR] SAMPLE_COMM_ISP_Run failed: %d\n", ret);
        return ret;
    }

    printf("[INFO] ISP initialized: cam=%d, iq=%s\n", ISP_CAM_ID, IQ_FILE_DIR);
    return 0;
}

static void isp_deinit(void) {
    SAMPLE_COMM_ISP_Stop(ISP_CAM_ID);
    printf("[INFO] ISP stopped\n");
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

    /* ---- Configure VI channel ---- */
    VI_CHN_ATTR_S chn_attr;
    memset(&chn_attr, 0, sizeof(chn_attr));
    chn_attr.stSize.u32Width       = VI_WIDTH;
    chn_attr.stSize.u32Height      = VI_HEIGHT;
    chn_attr.enPixelFormat         = RK_FMT_YUV420SP;   /* NV12 */
    chn_attr.u32Depth              = 2;                  /* User get-list depth */
    chn_attr.enCompressMode        = COMPRESS_MODE_NONE;
    chn_attr.stIspOpt.u32BufCount  = 2;
    chn_attr.stIspOpt.enMemoryType = VI_V4L2_MEMORY_TYPE_DMABUF;

    ret = RK_MPI_VI_SetChnAttr(pipe_id, VI_CHN_ID, &chn_attr);
    if (ret != 0) {
        printf("[ERROR] RK_MPI_VI_SetChnAttr failed: 0x%x\n", ret);
        return ret;
    }

    ret = RK_MPI_VI_EnableChn(pipe_id, VI_CHN_ID);
    if (ret != 0) {
        printf("[ERROR] RK_MPI_VI_EnableChn failed: 0x%x\n", ret);
        return ret;
    }

    printf("[INFO] VI initialized: pipe=%d, chn=%d, %dx%d NV12\n",
           pipe_id, VI_CHN_ID, VI_WIDTH, VI_HEIGHT);
    return 0;
}

static void vi_deinit(void) {
    RK_MPI_VI_DisableChn(VI_PIPE_ID, VI_CHN_ID);
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
    chn0_attr.u32Depth                     = 1;
    chn0_attr.u32FrameBufCnt               = 3;

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

    /* ---- Channel 1: 320x320 NV12 reserved for NPU ---- */
    VPSS_CHN_ATTR_S chn1_attr;
    memset(&chn1_attr, 0, sizeof(chn1_attr));
    chn1_attr.enChnMode                    = VPSS_CHN_MODE_AUTO;
    chn1_attr.u32Width                     = NPU_WIDTH;
    chn1_attr.u32Height                    = NPU_HEIGHT;
    chn1_attr.enPixelFormat                = RK_FMT_YUV420SP;  /* NV12 */
    chn1_attr.enCompressMode               = COMPRESS_MODE_NONE;
    chn1_attr.stFrameRate.s32SrcFrameRate  = -1;
    chn1_attr.stFrameRate.s32DstFrameRate  = -1;
    chn1_attr.u32Depth                     = 1;
    chn1_attr.u32FrameBufCnt               = 2;

    ret = RK_MPI_VPSS_SetChnAttr(VPSS_GRP_ID, VPSS_CHN_NPU, &chn1_attr);
    if (ret != 0) {
        printf("[ERROR] RK_MPI_VPSS_SetChnAttr(CHN1) failed: 0x%x\n", ret);
        return ret;
    }

    ret = RK_MPI_VPSS_EnableChn(VPSS_GRP_ID, VPSS_CHN_NPU);
    if (ret != 0) {
        printf("[ERROR] RK_MPI_VPSS_EnableChn(CHN1) failed: 0x%x\n", ret);
        return ret;
    }

    /* ---- Start VPSS group ---- */
    ret = RK_MPI_VPSS_StartGrp(VPSS_GRP_ID);
    if (ret != 0) {
        printf("[ERROR] RK_MPI_VPSS_StartGrp failed: 0x%x\n", ret);
        return ret;
    }

    printf("[INFO] VPSS initialized: grp=%d\n", VPSS_GRP_ID);
    printf("[INFO]   CHN0 (display): %dx%d NV12\n", DISPLAY_WIDTH, DISPLAY_HEIGHT);
    printf("[INFO]   CHN1 (npu):     %dx%d NV12\n", NPU_WIDTH, NPU_HEIGHT);
    return 0;
}

static void vpss_deinit(void) {
    /* Disable channels before stopping group (RKMPI requirement) */
    RK_MPI_VPSS_DisableChn(VPSS_GRP_ID, VPSS_CHN_NPU);
    RK_MPI_VPSS_DisableChn(VPSS_GRP_ID, VPSS_CHN_DISPLAY);
    RK_MPI_VPSS_StopGrp(VPSS_GRP_ID);
    RK_MPI_VPSS_DestroyGrp(VPSS_GRP_ID);
    printf("[INFO] VPSS deinitialized\n");
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

    /* ---- VO channel ---- */
    VO_CHN_ATTR_S chn_attr;
    memset(&chn_attr, 0, sizeof(chn_attr));
    chn_attr.stRect.s32X      = 0;
    chn_attr.stRect.s32Y      = 0;
    chn_attr.stRect.u32Width  = VO_SCREEN_WIDTH;
    chn_attr.stRect.u32Height = VO_SCREEN_HEIGHT;
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

    /* VPSS CHN0 -> VO */
    ret = RK_MPI_SYS_Bind(&g_vpss_chn_display, &g_vo_chn);
    if (ret != 0) {
        printf("[ERROR] Bind VPSS -> VO failed: 0x%x\n", ret);
        return ret;
    }
    printf("[INFO] Bound VPSS(grp=%d,chn=%d) -> VO(layer=%d,chn=%d)\n",
           VPSS_GRP_ID, VPSS_CHN_DISPLAY, VO_LAYER_ID, VO_CHN_ID);

    return 0;
}

static void unbind_pipeline(void) {
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
        /*
         * Query VPSS channel 0 frame count to estimate throughput.
         * In a hardware-bound pipeline (VI->VPSS->VO), the actual FPS
         * is determined by the VI capture rate and VO refresh rate.
         *
         * For detailed FPS tracking, use RK_MPI_VPSS_GetChnFrame() in
         * a polling loop, but since we use bind mode the frames flow
         * automatically. We simply print a heartbeat here.
         */
        sleep(5);
        if (g_running) {
            printf("[INFO] Pipeline running... (target: %d FPS)\n", TARGET_FPS);
        }
    }

    printf("[INFO] FPS monitor thread exiting\n");
    return NULL;
}

/* --------------------------------------------------------------------------
 * Main
 * -------------------------------------------------------------------------- */

int main(int argc, char *argv[]) {
    (void)argc;
    (void)argv;

    int ret;
    pthread_t fps_tid;

    printf("============================================\n");
    printf("  AI-HUD Camera Display Pipeline\n");
    printf("  Platform: Luckfox Pico Ultra (RV1106G3)\n");
    printf("  Camera:   SC3336 MIPI CSI\n");
    printf("  Display:  DPI LCD %dx%d\n", VO_SCREEN_WIDTH, VO_SCREEN_HEIGHT);
    printf("  Target:   >= %d FPS\n", TARGET_FPS);
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

    ret = vo_init();
    if (ret != 0) {
        printf("[ERROR] VO init failed, aborting\n");
        goto cleanup_vpss;
    }

    /* ---- Step 3: Bind pipeline ---- */
    setup_bind_params();
    ret = bind_pipeline();
    if (ret != 0) {
        printf("[ERROR] Pipeline bind failed, aborting\n");
        goto cleanup_vo;
    }

    printf("[INFO] Pipeline started: VI -> VPSS -> VO\n");
    printf("[INFO] Press Ctrl+C to stop\n");

    /* ---- Step 4: Start FPS monitor thread ---- */
    int fps_thread_created = 0;
    ret = pthread_create(&fps_tid, NULL, fps_monitor_thread, NULL);
    if (ret != 0) {
        printf("[WARN] Failed to create FPS monitor thread: %s\n", strerror(ret));
        /* Non-fatal, continue without monitoring */
    } else {
        fps_thread_created = 1;
    }

    /* ---- Step 5: Main loop -- wait for exit signal ---- */
    /*
     * In bind mode, frames flow automatically through the hardware pipeline:
     *   VI captures -> VPSS scales -> VO displays
     * No manual frame get/release is needed for the display path.
     *
     * The NPU channel (VPSS CHN1) is available for a separate inference
     * thread to call RK_MPI_VPSS_GetChnFrame() / ReleaseChnFrame() on
     * VPSS_CHN_NPU when that module is implemented.
     */
    while (g_running) {
        usleep(100 * 1000);  /* 100ms idle poll */
    }

    printf("[INFO] Shutting down pipeline...\n");

    /* ---- Step 6: Cleanup (reverse order) ---- */
    if (fps_thread_created)
        pthread_join(fps_tid, NULL);

    unbind_pipeline();

cleanup_vo:
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
