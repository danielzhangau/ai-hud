/* hud_ipc.h -- IPC between NPU inference (C) and HUD display (Python).
 *
 * The C inference process writes detection results to a plain text file.
 * The Python HUD (hud_live.py) polls the file and updates the display.
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

#define HUD_IPC_FILE      "/tmp/ai_hud_detect"
#define HUD_IPC_FILE_TMP  "/tmp/ai_hud_detect.tmp"

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

#endif /* HUD_IPC_H */
