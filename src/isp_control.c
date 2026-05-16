/*
 * isp_control.c -- RKAIQ ISP control with dashcam-optimized auto-exposure.
 *
 * Uses rk_aiq_uapi2 directly (bypassing SAMPLE_COMM_ISP wrapper)
 * to retain the rk_aiq_sys_ctx_t* handle for runtime AE/DRC/NR tuning.
 *
 * ISP lifecycle:
 *   1) enumStaticMetas  -> discover sensor entity name
 *   2) sysctl_init      -> create RKAIQ context
 *   3) sysctl_prepare   -> configure working mode
 *   4) sysctl_start     -> begin 3A processing
 *   5) apply defaults   -> dashcam AE/DRC/NR tuning
 *   6) sysctl_stop      -> stop 3A
 *   7) sysctl_deinit    -> release context
 */

#include "isp_control.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <time.h>

#include "rk_aiq_user_api2_sysctl.h"
#include "rk_aiq_user_api2_imgproc.h"

/* --------------------------------------------------------------------------
 * Module state
 * -------------------------------------------------------------------------- */

static rk_aiq_sys_ctx_t *g_aiq_ctx  = NULL;
static int                g_cam_id   = 0;
static int                g_night_on = 0;      /* current night mode state */
static time_t             g_last_ipc_check = 0;

/* --------------------------------------------------------------------------
 * Dashcam tuning constants
 *
 * These values are tuned for forward-facing dashcam use:
 *   - Speed sign recognition at 30-120m range
 *   - Mixed lighting: daylight, tunnel, night with oncoming headlights
 *   - SC3336 sensor (3MP, good SNR, ~1.25um pixel)
 * -------------------------------------------------------------------------- */

/*
 * Anti-flicker: auto-detect mode (handles 50Hz AU/CN mains).
 * Prevents LED traffic sign and streetlight flicker banding.
 * AUTO mode lets the ISP detect the flicker frequency from the scene.
 */
#define DASHCAM_ANTIFLICKER_MODE    ANTIFLICKER_AUTO_MODE

/*
 * AE gain limits (sensor analog + digital combined).
 * Unit: linear gain multiplier (1.0 = no gain, 64.0 = ~36dB).
 *
 * Day mode:  max 32x (~30dB) -- balanced noise/brightness
 * Night mode: max 64x (~36dB) -- prioritize visibility over noise
 *
 * SC3336 supports up to ~64x analog gain; beyond that, noise
 * degrades sign digit recognition significantly.
 */
#define GAIN_MIN                1.0f
#define GAIN_MAX_DAY            32.0f
#define GAIN_MAX_NIGHT          64.0f

/*
 * DRC (Dynamic Range Compression) -- tone-maps the ISP output
 * to handle high-contrast scenes (headlights vs dark road).
 *
 * LocalWeit:       local tone mapping weight (0.0-1.0)
 * GlobalContrast:  global contrast (0.0-1.0)
 * LoLitContrast:   low-light area contrast boost (0.0-1.0)
 *
 * Higher LoLitContrast = brighter shadows (better for sign visibility
 * in dark areas, but can amplify noise).
 */
#define DRC_LOCAL_WEIT_DAY      0.5f
#define DRC_GLOBAL_CONTRAST_DAY 0.6f
#define DRC_LOLIT_CONTRAST_DAY  0.4f

#define DRC_LOCAL_WEIT_NIGHT    0.7f
#define DRC_GLOBAL_CONTRAST_NIGHT 0.5f
#define DRC_LOLIT_CONTRAST_NIGHT  0.7f

/*
 * Noise reduction strength (0-100 scale).
 *
 * Spatial NR:   reduces per-frame noise but can blur edges.
 *               Keep moderate to preserve sign digit edges.
 * Temporal NR:  averages across frames, very effective for static
 *               scenes (dashcam at constant speed). Can ghost on
 *               fast-moving objects but signs are mostly stationary.
 *
 * Night mode cranks temporal NR to suppress high-gain noise.
 */
#define SPATIAL_NR_DAY          40
#define TEMPORAL_NR_DAY         50

#define SPATIAL_NR_NIGHT        50
#define TEMPORAL_NR_NIGHT       80

/* --------------------------------------------------------------------------
 * Internal helpers
 * -------------------------------------------------------------------------- */

/*
 * Apply AE gain range limits.
 */
static int apply_gain_range(float max_gain)
{
    paRange_t gain = { .min = GAIN_MIN, .max = max_gain };
    XCamReturn ret = rk_aiq_uapi2_setExpGainRange(g_aiq_ctx, &gain);
    if (ret != 0) {
        printf("[ISP] WARN: setExpGainRange failed: %d\n", ret);
        return -1;
    }
    printf("[ISP] AE gain range: %.1f - %.1f\n", gain.min, gain.max);
    return 0;
}

/*
 * Apply anti-flicker configuration.
 */
static int apply_antiflicker(void)
{
    XCamReturn ret;

    ret = rk_aiq_uapi2_setAntiFlickerEn(g_aiq_ctx, true);
    if (ret != 0) {
        printf("[ISP] WARN: setAntiFlickerEn failed: %d\n", ret);
        return -1;
    }

    ret = rk_aiq_uapi2_setAntiFlickerMode(g_aiq_ctx, DASHCAM_ANTIFLICKER_MODE);
    if (ret != 0) {
        printf("[ISP] WARN: setAntiFlickerMode failed: %d\n", ret);
        return -1;
    }

    printf("[ISP] Anti-flicker: ON, auto-detect\n");
    return 0;
}

/*
 * Apply DRC (Dynamic Range Compression) settings.
 *
 * Uses setDrcLocalData (with auto-enable) first, which is the
 * complete API supported by ISP32.  Falls back to setDrcLocalTMO
 * if setDrcLocalData is not available.
 */
static int apply_drc(float local_weit, float global_contrast, float lolit_contrast)
{
    XCamReturn ret;

    /* Try full API with auto mode enabled (ISP32 preferred) */
    ret = rk_aiq_uapi2_setDrcLocalData(
        g_aiq_ctx, local_weit, global_contrast, lolit_contrast,
        1,      /* LocalAutoEnable = true */
        1.0f);  /* LocalAutoWeit = 1.0 (full auto weight) */
    if (ret == 0) {
        printf("[ISP] DRC: local=%.2f global=%.2f lolit=%.2f (auto)\n",
               local_weit, global_contrast, lolit_contrast);
        return 0;
    }
    printf("[ISP] WARN: setDrcLocalData failed: %d, trying setDrcLocalTMO\n", ret);

    /* Fallback: simpler TMO-only API */
    ret = rk_aiq_uapi2_setDrcLocalTMO(
        g_aiq_ctx, local_weit, global_contrast, lolit_contrast);
    if (ret == 0) {
        printf("[ISP] DRC: local=%.2f global=%.2f lolit=%.2f (TMO)\n",
               local_weit, global_contrast, lolit_contrast);
        return 0;
    }
    printf("[ISP] WARN: DRC not available on this ISP (err=%d), skipping\n", ret);
    return -1;
}

/*
 * Apply noise reduction settings.
 */
static int apply_nr(unsigned int spatial, unsigned int temporal)
{
    XCamReturn ret;

    ret = rk_aiq_uapi2_setMSpaNRStrth(g_aiq_ctx, true, spatial);
    if (ret != 0)
        printf("[ISP] WARN: setMSpaNRStrth failed: %d\n", ret);

    ret = rk_aiq_uapi2_setMTNRStrth(g_aiq_ctx, true, temporal);
    if (ret != 0)
        printf("[ISP] WARN: setMTNRStrth failed: %d\n", ret);

    printf("[ISP] NR: spatial=%u temporal=%u\n", spatial, temporal);
    return 0;
}

/* --------------------------------------------------------------------------
 * Public API
 * -------------------------------------------------------------------------- */

int isp_ctrl_init(int cam_id, const char *iq_dir)
{
    XCamReturn ret;
    g_cam_id = cam_id;

    /* Step 1: Enumerate sensor to get entity name */
    rk_aiq_static_info_t static_info;
    memset(&static_info, 0, sizeof(static_info));

    ret = rk_aiq_uapi2_sysctl_enumStaticMetas(cam_id, &static_info);
    if (ret != 0) {
        printf("[ISP] ERROR: enumStaticMetas(cam=%d) failed: %d\n",
               cam_id, ret);
        return -1;
    }

    const char *sns_name = static_info.sensor_info.sensor_name;
    printf("[ISP] Sensor discovered: cam=%d, entity='%s'\n",
           cam_id, sns_name);
    fflush(stdout);

    /* Step 2: Initialize RKAIQ context */
    g_aiq_ctx = rk_aiq_uapi2_sysctl_init(sns_name, iq_dir, NULL, NULL);
    if (!g_aiq_ctx) {
        printf("[ISP] ERROR: sysctl_init failed\n");
        fflush(stdout);
        return -1;
    }
    printf("[ISP] sysctl_init: ok\n");
    fflush(stdout);

    /* Step 3: Prepare (0,0 = use sensor default resolution) */
    ret = rk_aiq_uapi2_sysctl_prepare(g_aiq_ctx, 0, 0,
                                       RK_AIQ_WORKING_MODE_NORMAL);
    if (ret != 0) {
        printf("[ISP] ERROR: sysctl_prepare failed: %d\n", ret);
        fflush(stdout);
        rk_aiq_uapi2_sysctl_deinit(g_aiq_ctx);
        g_aiq_ctx = NULL;
        return -1;
    }
    printf("[ISP] sysctl_prepare: ok (mode=NORMAL)\n");
    fflush(stdout);

    /* Step 4: Start 3A processing loop */
    ret = rk_aiq_uapi2_sysctl_start(g_aiq_ctx);
    if (ret != 0) {
        printf("[ISP] ERROR: sysctl_start failed: %d\n", ret);
        fflush(stdout);
        rk_aiq_uapi2_sysctl_deinit(g_aiq_ctx);
        g_aiq_ctx = NULL;
        return -1;
    }
    printf("[ISP] sysctl_start: ok\n");
    fflush(stdout);

    printf("[ISP] Initialized: cam=%d, iq=%s, mode=NORMAL\n",
           cam_id, iq_dir);
    fflush(stdout);
    return 0;
}

int isp_ctrl_apply_defaults(void)
{
    if (!g_aiq_ctx) {
        printf("[ISP] ERROR: apply_defaults called before init\n");
        return -1;
    }

    printf("[ISP] Applying dashcam-optimized defaults...\n");
    fflush(stdout);

    /*
     * Each tuning step is independent and best-effort.
     * Some uAPI2 functions may not be available on all ISP versions;
     * failures are logged but do not abort the pipeline.
     */

    /* Ensure auto-exposure mode */
    XCamReturn ret = rk_aiq_uapi2_setExpMode(g_aiq_ctx, OP_AUTO);
    printf("[ISP] setExpMode(AUTO): %s\n", ret == 0 ? "ok" : "failed");
    fflush(stdout);

    /* Anti-flicker (auto-detect 50Hz for AU + CN) */
    apply_antiflicker();
    fflush(stdout);

    /* AE gain range (day mode default) */
    apply_gain_range(GAIN_MAX_DAY);
    fflush(stdout);

    /* DRC for high-contrast scenes */
    apply_drc(DRC_LOCAL_WEIT_DAY, DRC_GLOBAL_CONTRAST_DAY,
              DRC_LOLIT_CONTRAST_DAY);
    fflush(stdout);

    /* Noise reduction */
    apply_nr(SPATIAL_NR_DAY, TEMPORAL_NR_DAY);
    fflush(stdout);

    g_night_on = 0;
    printf("[ISP] Dashcam defaults applied (day mode)\n");
    fflush(stdout);
    return 0;
}

int isp_ctrl_set_night_mode(int enabled)
{
    if (!g_aiq_ctx) {
        printf("[ISP] ERROR: set_night_mode called before init\n");
        return -1;
    }

    if (enabled == g_night_on)
        return 0;  /* no change */

    g_night_on = enabled;

    if (enabled) {
        printf("[ISP] Switching to NIGHT mode\n");
        apply_gain_range(GAIN_MAX_NIGHT);
        apply_drc(DRC_LOCAL_WEIT_NIGHT, DRC_GLOBAL_CONTRAST_NIGHT,
                  DRC_LOLIT_CONTRAST_NIGHT);
        apply_nr(SPATIAL_NR_NIGHT, TEMPORAL_NR_NIGHT);
    } else {
        printf("[ISP] Switching to DAY mode\n");
        apply_gain_range(GAIN_MAX_DAY);
        apply_drc(DRC_LOCAL_WEIT_DAY, DRC_GLOBAL_CONTRAST_DAY,
                  DRC_LOLIT_CONTRAST_DAY);
        apply_nr(SPATIAL_NR_DAY, TEMPORAL_NR_DAY);
    }

    return 0;
}

void isp_ctrl_poll_ipc(void)
{
    if (!g_aiq_ctx)
        return;

    /* Throttle: check at most every ISP_CONFIG_POLL_SEC */
    time_t now = time(NULL);
    if (now - g_last_ipc_check < ISP_CONFIG_POLL_SEC)
        return;
    g_last_ipc_check = now;

    FILE *fp = fopen(ISP_CONFIG_IPC_FILE, "r");
    if (!fp)
        return;

    char line[64];
    while (fgets(line, sizeof(line), fp)) {
        int val;
        if (sscanf(line, "night_mode=%d", &val) == 1) {
            isp_ctrl_set_night_mode(val != 0);
        }
        /* Future: add more IPC keys here (e.g. brightness=N) */
    }
    fclose(fp);
}

void isp_ctrl_deinit(void)
{
    if (!g_aiq_ctx) {
        printf("[ISP] Already deinitialized\n");
        return;
    }

    rk_aiq_uapi2_sysctl_stop(g_aiq_ctx, false);
    rk_aiq_uapi2_sysctl_deinit(g_aiq_ctx);
    g_aiq_ctx = NULL;

    printf("[ISP] Stopped and deinitialized\n");
}
