/*
 * frame_capture.h -- On-device frame capture for model iteration.
 *
 * Saves raw NV12 frames + JSON metadata when the NPU detects a sign
 * or periodically while driving. Captured data is pulled offline,
 * converted, labeled, and fed back into the training pipeline.
 *
 * Storage: NV12 raw (zero encode cost on ARM) + JSON sidecar.
 * FIFO eviction when exceeding max frame count.
 *
 * Enable at runtime via IPC file /tmp/ai_hud_capture_enable ("1"/"0").
 * Disabled by default to avoid filling storage unintentionally.
 */

#ifndef FRAME_CAPTURE_H
#define FRAME_CAPTURE_H

#include "postprocess.h"
#include <stdint.h>

/* Default capture directory on the board */
#define CAPTURE_DIR_DEFAULT         "/root/capture"
#define CAPTURE_MAX_FRAMES_DEFAULT  500

/* Trigger thresholds */
#define CAPTURE_PERIODIC_SEC        30      /* Periodic capture interval (driving) */
#define CAPTURE_DETECT_COOLDOWN_MS  5000    /* Min ms between detection-triggered saves */
#define CAPTURE_MIN_CONF            0.15f   /* Min confidence to trigger detection save */
#define CAPTURE_MIN_SPEED_KMH       5.0f    /* Min speed for periodic capture */

/* IPC: Python -> C enable/disable toggle */
#define CAPTURE_ENABLE_IPC_FILE     "/tmp/ai_hud_capture_enable"

/* IPC: Python -> C GPS coordinates for metadata */
#define CAPTURE_GPS_IPC_FILE        "/tmp/ai_hud_gps"

/* Trigger type constants */
#define CAPTURE_TRIG_DETECT    0
#define CAPTURE_TRIG_PERIODIC  1
#define CAPTURE_TRIG_MANUAL    2

/*
 * Initialize capture module.
 *
 * Creates output_dir if needed, scans for existing frames.
 * Pass NULL for output_dir to use CAPTURE_DIR_DEFAULT.
 * Pass 0 for max_frames to use CAPTURE_MAX_FRAMES_DEFAULT.
 *
 * Returns 0 on success, -1 on error.
 */
int capture_init(const char *output_dir, int max_frames);

/*
 * Check trigger conditions and save frame if warranted.
 *
 * Called after each inference cycle. Handles detection-triggered
 * and periodic saves internally. Respects enable/disable IPC toggle.
 *
 * Returns 1 if frame was saved, 0 if skipped, -1 on error.
 */
int capture_check_and_save(const uint8_t *nv12_data, int width, int height,
                           const detect_result_group_t *results,
                           float speed_kmh);

/*
 * Check if capture is enabled via IPC file.
 * Returns 1 (enabled) or 0 (disabled). Defaults to 0 if file missing.
 */
int capture_is_enabled(void);

/*
 * Release capture module resources.
 */
void capture_release(void);

#endif /* FRAME_CAPTURE_H */
