/*
 * overlay_draw.h
 *
 * Framebuffer overlay for NPU detection bounding boxes and labels.
 * Draws directly into mmap'd XRGB8888 framebuffer (/dev/fb0).
 *
 * Coordinate mapping: NPU model space (640x640) -> display (480x480).
 * Both VI CHN1 (selfpath) and VPSS stretch 2304x1296 to their target
 * resolution, so the mapping is a uniform 480/640 = 0.75 scale.
 */

#ifndef OVERLAY_DRAW_H
#define OVERLAY_DRAW_H

#include <stdint.h>
#include "postprocess.h"

#ifdef __cplusplus
extern "C" {
#endif

/*
 * overlay_draw_detections - Draw bounding boxes + labels onto framebuffer.
 *
 * Maps detection box coordinates from NPU space (640x640) to display
 * space (480x480), draws colored outlines and speed labels.
 *
 * Box color indicates confidence (only >= 0.50 shown):
 *   Green  (>= 0.7)  High confidence
 *   Yellow (>= 0.5)  Medium confidence
 *
 * @fb:    Pointer to mmap'd XRGB8888 framebuffer
 * @fb_w:  Framebuffer width in pixels (480)
 * @fb_h:  Framebuffer height in pixels (480)
 * @dets:  Detection results from NPU inference
 */
void overlay_draw_detections(uint8_t *fb, int fb_w, int fb_h,
                             const detect_result_group_t *dets);

#ifdef __cplusplus
}
#endif

#endif /* OVERLAY_DRAW_H */
