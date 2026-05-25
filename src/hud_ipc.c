/* hud_ipc.c -- Definitions for IPC data shared across translation units. */

#include "hud_ipc.h"

/* Speed limit class-to-value mapping (11 classes, AU + CN combined).
 * Declared extern in hud_ipc.h, single definition here. */
const int SIGN_SPEEDS[] = {20, 30, 40, 50, 60, 70, 80, 90, 100, 110, 120};

/* ---------------------------------------------------------------------------
 * Smoothing ring buffer for hud_ipc_update_smoothed()
 *
 * State lives at file scope so the single NPU inference thread accumulates
 * a coherent history across calls. The buffer is intentionally tiny --
 * HUD_IPC_SMOOTH_WIN=5 entries -- because the goal is killing 1-2 frame
 * phantoms, not building a long-running tracker (the Python fusion layer
 * handles longer-term confirmation).
 *
 * sign_history holds the per-frame "best" sign limit (km/h) or 0 for
 * "no qualifying sign this frame". Storing 0 entries (rather than just
 * skipping them) is what makes a vote require >=MATCH out of the LAST
 * WIN frames -- a stable real sign holds the majority against a brief
 * gap, while a stray phantom doesn't see its slot replenished.
 *
 * sign_max_conf tracks the highest confidence ever seen for the winning
 * value within the current window, so the Python side gets a meaningful
 * confidence number to gate against its own threshold.
 * --------------------------------------------------------------------------- */

#include <string.h>  /* memset */

static int   smooth_sign_history[HUD_IPC_SMOOTH_WIN];
static float smooth_sign_conf[HUD_IPC_SMOOTH_WIN];
static int   smooth_camera_history[HUD_IPC_SMOOTH_WIN];
static int   smooth_idx     = 0;  /* next write position */
static int   smooth_filled  = 0;  /* number of valid entries (0..WIN) */

static int smooth_count_for(int value)
{
    int n = 0;
    for (int i = 0; i < smooth_filled; i++)
        if (smooth_sign_history[i] == value)
            n++;
    return n;
}

static float smooth_best_conf_for(int value)
{
    float best = 0.0f;
    for (int i = 0; i < smooth_filled; i++) {
        if (smooth_sign_history[i] == value && smooth_sign_conf[i] > best)
            best = smooth_sign_conf[i];
    }
    return best;
}

void hud_ipc_smoothed_reset(void)
{
    memset(smooth_sign_history, 0, sizeof(smooth_sign_history));
    memset(smooth_sign_conf, 0, sizeof(smooth_sign_conf));
    memset(smooth_camera_history, 0, sizeof(smooth_camera_history));
    smooth_idx = 0;
    smooth_filled = 0;
}

void hud_ipc_update_smoothed(
    int num_detections,
    const int *class_ids,
    const float *confidences)
{
    /* Step 1: extract per-frame best sign + camera flag (same logic as
     * hud_ipc_update_from_detections so calibration stays comparable). */
    int   frame_sign_limit = 0;
    float frame_sign_conf  = 0.0f;
    int   frame_camera     = 0;
    float frame_camera_conf = 0.0f;

    for (int i = 0; i < num_detections; i++) {
        int cls = class_ids[i];
        float conf = confidences[i];

        if (cls >= 0 && cls < SIGN_SPEED_COUNT) {
            if (conf > frame_sign_conf) {
                frame_sign_conf = conf;
                frame_sign_limit = SIGN_SPEEDS[cls];
            }
        } else if (cls == CLASS_SPEED_CAMERA) {
            frame_camera = 1;
            if (conf > frame_camera_conf)
                frame_camera_conf = conf;
        }
    }

    /* Step 2: append to ring buffer */
    smooth_sign_history[smooth_idx] = frame_sign_limit;
    smooth_sign_conf[smooth_idx]    = frame_sign_conf;
    smooth_camera_history[smooth_idx] = frame_camera;
    smooth_idx = (smooth_idx + 1) % HUD_IPC_SMOOTH_WIN;
    if (smooth_filled < HUD_IPC_SMOOTH_WIN)
        smooth_filled++;

    /* Step 3: find the dominant non-zero value in the window.
     *
     * Linear scan over a tiny buffer (5) -- no need for a map. We
     * accept the FIRST value whose count >= MATCH so a clear winner
     * always wins; ties pick the most-recent insertion (acceptable
     * because adjacent speeds rarely both reach MATCH within 5 frames).
     */
    int   winner_value = 0;
    int   winner_count = 0;
    for (int i = 0; i < smooth_filled; i++) {
        int v = smooth_sign_history[i];
        if (v == 0)
            continue;
        int n = smooth_count_for(v);
        if (n > winner_count) {
            winner_value = v;
            winner_count = n;
        }
    }

    int   smoothed_speed_limit = 0;
    float smoothed_confidence  = 0.0f;
    if (winner_count >= HUD_IPC_SMOOTH_MATCH) {
        smoothed_speed_limit = winner_value;
        smoothed_confidence  = smooth_best_conf_for(winner_value);
    }

    /* Step 4: camera flag passes through un-smoothed (current frame
     * only). Speed-camera warnings prefer false positives over false
     * negatives -- a warned-but-clear driver just eases off, whereas a
     * missed warning at 100 km/h costs a fine or worse. */
    float ipc_conf = smoothed_confidence > frame_camera_conf
                     ? smoothed_confidence : frame_camera_conf;
    hud_ipc_write(smoothed_speed_limit, frame_camera, ipc_conf);
}
