/* hud_ipc.c -- Definitions for IPC data shared across translation units. */

#include "hud_ipc.h"
#include <string.h>

/* Speed limit class-to-value mapping (11 classes, AU + CN combined). */
const int SIGN_SPEEDS[] = {20, 30, 40, 50, 60, 70, 80, 90, 100, 110, 120};

/* ---------------------------------------------------------------------------
 * Smoothing ring buffer for hud_ipc_update_smoothed().
 * State is file-scope static; safe only under the single NPU thread.
 * See hud_ipc.h for the algorithm + composition with the Python window.
 * --------------------------------------------------------------------------- */

static int   smooth_sign_history[HUD_IPC_SMOOTH_WIN];
static float smooth_sign_conf[HUD_IPC_SMOOTH_WIN];
static int   smooth_idx    = 0;
static int   smooth_filled = 0;

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
    smooth_idx = 0;
    smooth_filled = 0;
}

void hud_ipc_update_smoothed(
    int num_detections,
    const int *class_ids,
    const float *confidences)
{
    int frame_sign_limit, frame_camera;
    float frame_sign_conf, frame_camera_conf;
    hud_ipc_pick_frame_top(num_detections, class_ids, confidences,
                           &frame_sign_limit, &frame_sign_conf,
                           &frame_camera, &frame_camera_conf);

    smooth_sign_history[smooth_idx] = frame_sign_limit;
    smooth_sign_conf[smooth_idx]    = frame_sign_conf;
    smooth_idx = (smooth_idx + 1) % HUD_IPC_SMOOTH_WIN;
    if (smooth_filled < HUD_IPC_SMOOTH_WIN)
        smooth_filled++;

    /* Find the dominant non-zero value. Linear scan over a 5-entry
     * buffer; accept the first value reaching MATCH so a clear winner
     * always wins and ties pick the most-recent insertion. */
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

    float ipc_conf = smoothed_confidence > frame_camera_conf
                     ? smoothed_confidence : frame_camera_conf;
    hud_ipc_write(smoothed_speed_limit, frame_camera, ipc_conf);
}
