/*
 * isp_control.h -- RKAIQ ISP control with dashcam-optimized auto-exposure.
 *
 * Replaces SAMPLE_COMM_ISP wrapper to gain direct access to
 * rk_aiq_sys_ctx_t* for runtime AE/DRC/NR tuning via uAPI2.
 *
 * Dashcam defaults applied after ISP start:
 *   - Anti-flicker 50Hz (AU + CN mains frequency)
 *   - DRC (Dynamic Range Compression) for headlight vs dark road
 *   - Moderate spatial + temporal noise reduction
 *   - Auto-exposure with dashcam-tuned gain limits
 *
 * Night mode (toggled via IPC from Python settings UI):
 *   - Raises max sensor gain for low-light visibility
 *   - Strengthens DRC low-light contrast boost
 *   - Increases temporal NR to suppress high-gain noise
 */

#ifndef ISP_CONTROL_H
#define ISP_CONTROL_H

/* IPC file for Python -> C ISP configuration */
#define ISP_CONFIG_IPC_FILE     "/tmp/ai_hud_isp_config"

/* IPC poll interval (seconds) -- how often C checks for config changes */
#define ISP_CONFIG_POLL_SEC     2

/*
 * Initialize and start the ISP pipeline.
 *
 * Enumerates the sensor (SC3336), initializes RKAIQ, and starts
 * the 3A processing loop.  Must be called before VI init.
 *
 * cam_id:    Camera index (0 for single-camera setup)
 * iq_dir:    IQ tuning file directory (e.g. "/etc/iqfiles")
 *
 * Returns 0 on success, -1 on error.
 */
int isp_ctrl_init(int cam_id, const char *iq_dir);

/*
 * Apply dashcam-optimized ISP defaults.
 *
 * Call once after isp_ctrl_init() and before the main loop.
 * Configures anti-flicker, DRC, NR, and AE gain limits.
 *
 * Returns 0 on success, -1 on error.
 */
int isp_ctrl_apply_defaults(void);

/*
 * Toggle night mode.
 *
 * Night mode raises max gain, strengthens DRC and temporal NR
 * for improved low-light recognition at the cost of more noise.
 *
 * enabled:  1 = night mode ON, 0 = normal (daytime) mode
 *
 * Returns 0 on success, -1 on error.
 */
int isp_ctrl_set_night_mode(int enabled);

/*
 * Check IPC file for configuration changes.
 *
 * Reads ISP_CONFIG_IPC_FILE and applies any changed settings.
 * Call periodically from a monitoring thread (every ISP_CONFIG_POLL_SEC).
 *
 * Throttled internally -- safe to call more frequently.
 */
void isp_ctrl_poll_ipc(void);

/*
 * Stop and deinitialize the ISP pipeline.
 *
 * Must be called after VI deinit, in reverse startup order.
 */
void isp_ctrl_deinit(void);

#endif /* ISP_CONTROL_H */
