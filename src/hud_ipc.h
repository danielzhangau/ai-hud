/* hud_ipc.h -- IPC between NPU inference (C) and HUD display (Python).
 *
 * Two IPC channels:
 *   C -> Python: detection results (speed_limit, camera, confidence)
 *   Python -> C: vehicle speed (for adaptive inference rate)
 *
 * File format (one key=value per line):
 *   speed_limit=60
 *   camera=1
 *   confidence=0.85
 *   timestamp=1234567890
 *
 * Write protocol:
 *   1. Write to a temp file (IPC_FILE ".tmp")
 *   2. rename() to IPC_FILE (atomic on same filesystem)
 *   This ensures the reader never sees a partial write.
 */

#ifndef HUD_IPC_H
#define HUD_IPC_H

#include <stdio.h>
#include <time.h>

/* C -> Python: NPU detection results */
#define HUD_IPC_FILE      "/tmp/ai_hud_detect"
#define HUD_IPC_FILE_TMP  "/tmp/ai_hud_detect.tmp"

/* Python -> C: vehicle speed for adaptive inference rate */
#define HUD_IPC_SPEED_FILE "/tmp/ai_hud_speed"

/* Python -> C: NPU enable/disable toggle (content: "1" or "0") */
#define HUD_IPC_NPU_ENABLE_FILE "/tmp/ai_hud_npu_enable"

/* Python -> C: display mode ("cam\n" = camera, file absent = HUD) */
#define HUD_IPC_DISPLAY_MODE "/tmp/ai_hud_display_mode"

/* Universal speed limit classes (matching postprocess.h class order)
 * 11 classes covering both AU and CN speed limits.
 * Device-side region filtering is handled in Python (hud_live.py). */
extern const int SIGN_SPEEDS[];
#define SIGN_SPEED_COUNT  11
#define CLASS_SPEED_CAMERA -1  /* Speed camera detected via GPS database, not vision */

/*
 * Write detection result to IPC file (atomic via rename).
 *
 * speed_limit: detected speed limit in km/h (0 = no sign detected)
 * camera:      1 if speed camera detected, 0 otherwise
 * confidence:  highest detection confidence [0.0, 1.0]
 *
 * Returns 0 on success, -1 on error.
 */
static inline int hud_ipc_write(int speed_limit, int camera, float confidence)
{
    FILE *fp = fopen(HUD_IPC_FILE_TMP, "w");
    if (!fp)
        return -1;

    fprintf(fp, "speed_limit=%d\n", speed_limit);
    fprintf(fp, "camera=%d\n", camera);
    fprintf(fp, "confidence=%.2f\n", confidence);
    fprintf(fp, "timestamp=%ld\n", (long)time(NULL));

    /* Flush user-space buffer before close to ensure data hits the
     * filesystem before the subsequent rename().  On tmpfs this is
     * mostly a no-op, but prevents data loss if backed by real storage. */
    fflush(fp);
    fclose(fp);

    /* Atomic replace -- reader never sees partial content */
    if (rename(HUD_IPC_FILE_TMP, HUD_IPC_FILE) != 0)
        return -1;

    return 0;
}

/*
 * Pick the top sign + top camera detection from a frame's raw output.
 * Out params receive 0 / 0.0f when no detection of that kind passed.
 */
static inline void hud_ipc_pick_frame_top(
    int num_detections,
    const int *class_ids,
    const float *confidences,
    int *sign_limit, float *sign_conf,
    int *camera, float *camera_conf)
{
    *sign_limit = 0; *sign_conf = 0.0f;
    *camera = 0;     *camera_conf = 0.0f;
    for (int i = 0; i < num_detections; i++) {
        int cls = class_ids[i];
        float conf = confidences[i];
        if (cls >= 0 && cls < SIGN_SPEED_COUNT) {
            if (conf > *sign_conf) {
                *sign_conf = conf;
                *sign_limit = SIGN_SPEEDS[cls];
            }
        } else if (cls == CLASS_SPEED_CAMERA) {
            *camera = 1;
            if (conf > *camera_conf)
                *camera_conf = conf;
        }
    }
}

/*
 * Raw variant: writes the per-frame top detection straight to IPC
 * without inter-frame voting. Kept for debugging / tooling that wants
 * the unfiltered stream; the inference loop should call
 * hud_ipc_update_smoothed() instead.
 */
static inline void hud_ipc_update_from_detections(
    int num_detections,
    const int *class_ids,
    const float *confidences)
{
    int sign_limit, camera;
    float sign_conf, camera_conf;
    hud_ipc_pick_frame_top(num_detections, class_ids, confidences,
                           &sign_limit, &sign_conf, &camera, &camera_conf);
    float top_conf = sign_conf > camera_conf ? sign_conf : camera_conf;
    hud_ipc_write(sign_limit, camera, top_conf);
}

/*
 * Smoothed variant: N-of-M sliding-window vote over the last
 * HUD_IPC_SMOOTH_WIN inference frames. Composes with the Python-side
 * 4-of-6 fusion window to give two-stage hysteresis:
 *   C ring  (5 frames, 3 match): 600 ms-3 s accept depending on FPS
 *   Python  (6 polls,  4 match): adds ~3-4 s confirmation at 1 Hz RMC
 *
 * On winner_count < HUD_IPC_SMOOTH_MATCH writes speed_limit=0 so the
 * Python window naturally treats that poll as absent.
 *
 * Camera flag is NOT smoothed -- transient alerts favor false positives
 * over false negatives (a wrongly-warned driver just eases off).
 *
 * Thread-safety: file-scope static state in hud_ipc.c, safe only under
 * the single NPU inference thread.
 */
#define HUD_IPC_SMOOTH_WIN    5
#define HUD_IPC_SMOOTH_MATCH  3

void hud_ipc_update_smoothed(
    int num_detections,
    const int *class_ids,
    const float *confidences);

/*
 * Reset the smoothing ring buffer. Call when:
 *   - NPU inference is disabled (via /tmp/ai_hud_npu_enable=0)
 *   - The display mode switches between HUD and CAM and you want
 *     a clean slate for the new mode.
 * Calling this clears past samples so the next 3 frames decide the
 * new winner from scratch.
 */
void hud_ipc_smoothed_reset(void);

/* ---------------------------------------------------------------------------
 * Python -> C: Read vehicle speed for adaptive inference rate
 * ---------------------------------------------------------------------------
 *
 * The Python HUD writes current GPS speed (km/h) to HUD_IPC_SPEED_FILE.
 * The C inference thread reads it to adjust inference interval.
 *
 * Returns speed in km/h, or -1.0 if file is unavailable.
 */
static inline float hud_ipc_read_speed(void)
{
    FILE *fp = fopen(HUD_IPC_SPEED_FILE, "r");
    if (!fp)
        return -1.0f;
    float speed = -1.0f;
    if (fscanf(fp, "%f", &speed) != 1)
        speed = -1.0f;
    fclose(fp);
    return speed;
}

/*
 * Compute adaptive inference sleep interval (milliseconds).
 *
 * Higher vehicle speed -> shorter interval (more frequent detection).
 * Returns the sleep duration AFTER inference completes.
 *
 * Speed brackets:
 *   0-5 km/h    -> 5000ms  (parked/idle, minimal detection)
 *   5-30 km/h   -> 2000ms  (city crawl, GPS DB covers most)
 *   30-60 km/h  -> 1000ms  (urban, moderate detection)
 *   60-100 km/h ->  500ms  (fast road, construction zones matter)
 *   100+ km/h   ->  200ms  (highway, max detection frequency)
 *   unknown     ->  500ms  (no GPS data, moderate default)
 */
static inline int hud_ipc_adaptive_sleep_ms(float speed_kmh, float infer_ms)
{
    int target_ms;

    if (speed_kmh < 0.0f) {
        target_ms = 500;    /* No speed data available */
    } else if (speed_kmh < 5.0f) {
        target_ms = 5000;   /* Parked / idle */
    } else if (speed_kmh < 30.0f) {
        target_ms = 2000;   /* City crawl */
    } else if (speed_kmh < 60.0f) {
        target_ms = 1000;   /* Urban */
    } else if (speed_kmh < 100.0f) {
        target_ms = 500;    /* Fast road */
    } else {
        target_ms = 200;    /* Highway */
    }

    /* Subtract inference time already spent */
    int sleep_ms = target_ms - (int)infer_ms;
    if (sleep_ms < 0)
        sleep_ms = 0;

    return sleep_ms;
}

/* ---------------------------------------------------------------------------
 * Python -> C: Read NPU enable/disable toggle
 * ---------------------------------------------------------------------------
 *
 * The Python HUD (or future settings UI) writes "1" or "0" to
 * HUD_IPC_NPU_ENABLE_FILE to toggle live NPU inference on/off.
 *
 * Returns 1 (enabled) or 0 (disabled). Defaults to 1 if file is missing.
 */
static inline int hud_ipc_read_npu_enabled(void)
{
    FILE *fp = fopen(HUD_IPC_NPU_ENABLE_FILE, "r");
    if (!fp)
        return 1;  /* Default: enabled */
    int val = 1;
    if (fscanf(fp, "%d", &val) != 1)
        val = 1;
    fclose(fp);
    return val != 0;
}

#endif /* HUD_IPC_H */
