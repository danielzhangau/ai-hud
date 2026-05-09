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

/* Speed limit classes (matching postprocess.h CLASS_LABELS) */
static const int SIGN_SPEEDS[] = {30, 40, 50, 60, 70, 80, 100, 110};
#define SIGN_SPEED_COUNT  8
#define CLASS_SPEED_CAMERA 8  /* class_id for speed camera */

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
    fclose(fp);

    /* Atomic replace -- reader never sees partial content */
    if (rename(HUD_IPC_FILE_TMP, HUD_IPC_FILE) != 0)
        return -1;

    return 0;
}

/*
 * Convert detection result (class_id + confidence) to IPC write.
 *
 * Typical usage in the inference loop:
 *   detect_result_group_t results;
 *   // ... run inference, get results ...
 *   hud_ipc_update_from_detections(&results);
 */
static inline void hud_ipc_update_from_detections(
    int num_detections,
    const int *class_ids,
    const float *confidences)
{
    int best_sign_limit = 0;
    float best_sign_conf = 0.0f;
    int camera = 0;
    float camera_conf = 0.0f;

    for (int i = 0; i < num_detections; i++) {
        int cls = class_ids[i];
        float conf = confidences[i];

        if (cls >= 0 && cls < SIGN_SPEED_COUNT) {
            /* Speed sign -- pick highest confidence */
            if (conf > best_sign_conf) {
                best_sign_conf = conf;
                best_sign_limit = SIGN_SPEEDS[cls];
            }
        } else if (cls == CLASS_SPEED_CAMERA) {
            camera = 1;
            if (conf > camera_conf)
                camera_conf = conf;
        }
    }

    float top_conf = best_sign_conf > camera_conf ? best_sign_conf : camera_conf;
    hud_ipc_write(best_sign_limit, camera, top_conf);
}

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

#endif /* HUD_IPC_H */
