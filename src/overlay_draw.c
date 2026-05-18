/*
 * overlay_draw.c
 *
 * Framebuffer overlay: bounding boxes + labels for NPU detections.
 * Draws directly into mmap'd XRGB8888 framebuffer (/dev/fb0).
 *
 * Features:
 *   - NPU (640x640) -> Display (480x480) coordinate mapping
 *   - Confidence-colored bounding box outlines (green/yellow, >= 0.50 only)
 *   - Embedded 5x7 bitmap font at 2x scale for speed labels
 *   - Label format: "60 85%" (speed value + confidence)
 */

#include "overlay_draw.h"
#include "hud_ipc.h"
#include "utils.h"

#include <string.h>
#include <stdio.h>

/* -----------------------------------------------------------------------
 * Result freshness window
 *
 * Display refresh (~25 FPS = 40ms) outpaces NPU inference (~4 FPS = 250ms)
 * in CAM mode, so the same detection result is consumed by ~6 display
 * frames. After STALE_THRESHOLD_MS without a fresh result we stop drawing
 * boxes -- they no longer match what the user sees on screen.
 *
 * 350ms gives us one full inference cycle of grace + headroom for jitter.
 * Tighter than this and we flicker; looser and stale boxes persist.
 * ----------------------------------------------------------------------- */
#define STALE_THRESHOLD_MS  350

/* -----------------------------------------------------------------------
 * Coordinate mapping: NPU (640x640) -> Display (480x480)
 *
 * Both paths apply the same non-uniform stretch (no center-crop):
 *   VI CHN1 selfpath: 2304x1296 -> 640x640 (stretch)
 *   VPSS CHN0:        2304x1296 -> 480x480 (stretch)
 *
 * Since both distort the sensor image identically (same aspect mapping),
 * the transform is a simple uniform scale: 480/640 = 0.75.
 * ----------------------------------------------------------------------- */

#define NPU_SZ      640
#define DISPLAY_SZ  480

#define MAP_SCALE   ((float)DISPLAY_SZ / (float)NPU_SZ)   /* 0.75 */

/* -----------------------------------------------------------------------
 * Drawing constants
 * ----------------------------------------------------------------------- */

#define BOX_THICK   2       /* Bounding box outline thickness (pixels)     */
#define FONT_SCALE  2       /* Bitmap font scale factor (2x = 10x14 chars) */
#define FONT_W      5       /* Base glyph width                            */
#define FONT_H      7       /* Base glyph height                           */
#define CHAR_W      (FONT_W * FONT_SCALE)       /* 10 px rendered width  */
#define CHAR_H      (FONT_H * FONT_SCALE)       /* 14 px rendered height */
#define CHAR_GAP    (1 * FONT_SCALE)             /* 2 px inter-char gap   */
#define LABEL_PAD   2       /* Padding around label text (pixels)          */

/* FB_BPP defined in utils.h (XRGB8888 = 4 bytes) */

/* -----------------------------------------------------------------------
 * Embedded 5x7 bitmap font (digits 0-9, space, '%')
 *
 * Each glyph: 7 rows, each row is 5 bits packed in a uint8_t.
 * Bit 4 = leftmost pixel, bit 0 = rightmost pixel.
 * ----------------------------------------------------------------------- */

#define GLYPH_COUNT 12

static const uint8_t font_5x7[GLYPH_COUNT][FONT_H] = {
    /* [0]  '0' */ {0x0E, 0x11, 0x13, 0x15, 0x19, 0x11, 0x0E},
    /* [1]  '1' */ {0x04, 0x0C, 0x04, 0x04, 0x04, 0x04, 0x0E},
    /* [2]  '2' */ {0x0E, 0x11, 0x01, 0x06, 0x08, 0x10, 0x1F},
    /* [3]  '3' */ {0x0E, 0x11, 0x01, 0x06, 0x01, 0x11, 0x0E},
    /* [4]  '4' */ {0x02, 0x06, 0x0A, 0x12, 0x1F, 0x02, 0x02},
    /* [5]  '5' */ {0x1F, 0x10, 0x1E, 0x01, 0x01, 0x11, 0x0E},
    /* [6]  '6' */ {0x06, 0x08, 0x10, 0x1E, 0x11, 0x11, 0x0E},
    /* [7]  '7' */ {0x1F, 0x01, 0x02, 0x04, 0x08, 0x08, 0x08},
    /* [8]  '8' */ {0x0E, 0x11, 0x11, 0x0E, 0x11, 0x11, 0x0E},
    /* [9]  '9' */ {0x0E, 0x11, 0x11, 0x0F, 0x01, 0x02, 0x0C},
    /* [10] ' ' */ {0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00},
    /* [11] '%' */ {0x00, 0x19, 0x1A, 0x04, 0x0B, 0x13, 0x00},
};

static int char_to_glyph(char c) {
    if (c >= '0' && c <= '9') return c - '0';
    if (c == ' ') return 10;
    if (c == '%') return 11;
    return -1;
}

/* -----------------------------------------------------------------------
 * Coordinate mapping helpers
 * ----------------------------------------------------------------------- */

static inline int map_x(int npu_x, int fb_w) {
    int dx = (int)((float)npu_x * MAP_SCALE + 0.5f);
    if (dx < 0) return 0;
    if (dx >= fb_w) return fb_w - 1;
    return dx;
}

static inline int map_y(int npu_y, int fb_h) {
    int dy = (int)((float)npu_y * MAP_SCALE + 0.5f);
    if (dy < 0) return 0;
    if (dy >= fb_h) return fb_h - 1;
    return dy;
}

/* -----------------------------------------------------------------------
 * Pixel-level drawing primitives
 * ----------------------------------------------------------------------- */

static inline void put_pixel(uint8_t *fb, int fb_w, int fb_h,
                              int x, int y,
                              uint8_t r, uint8_t g, uint8_t b) {
    if (x < 0 || x >= fb_w || y < 0 || y >= fb_h)
        return;
    int off = (y * fb_w + x) * FB_BPP;
    fb[off]     = b;
    fb[off + 1] = g;
    fb[off + 2] = r;
    fb[off + 3] = 0xFF;
}

static void draw_hline(uint8_t *fb, int fb_w, int fb_h,
                        int x0, int x1, int y,
                        uint8_t r, uint8_t g, uint8_t b) {
    if (y < 0 || y >= fb_h) return;
    if (x0 > x1) { int t = x0; x0 = x1; x1 = t; }
    if (x0 < 0) x0 = 0;
    if (x1 >= fb_w) x1 = fb_w - 1;

    for (int x = x0; x <= x1; x++) {
        int off = (y * fb_w + x) * FB_BPP;
        fb[off]     = b;
        fb[off + 1] = g;
        fb[off + 2] = r;
        fb[off + 3] = 0xFF;
    }
}

static void draw_vline(uint8_t *fb, int fb_w, int fb_h,
                        int x, int y0, int y1,
                        uint8_t r, uint8_t g, uint8_t b) {
    if (x < 0 || x >= fb_w) return;
    if (y0 > y1) { int t = y0; y0 = y1; y1 = t; }
    if (y0 < 0) y0 = 0;
    if (y1 >= fb_h) y1 = fb_h - 1;

    for (int y = y0; y <= y1; y++) {
        int off = (y * fb_w + x) * FB_BPP;
        fb[off]     = b;
        fb[off + 1] = g;
        fb[off + 2] = r;
        fb[off + 3] = 0xFF;
    }
}

/* Draw a rectangle outline with configurable thickness */
static void draw_rect(uint8_t *fb, int fb_w, int fb_h,
                       int x0, int y0, int x1, int y1, int thick,
                       uint8_t r, uint8_t g, uint8_t b) {
    for (int t = 0; t < thick; t++) {
        draw_hline(fb, fb_w, fb_h, x0, x1, y0 + t, r, g, b);  /* top    */
        draw_hline(fb, fb_w, fb_h, x0, x1, y1 - t, r, g, b);  /* bottom */
        draw_vline(fb, fb_w, fb_h, x0 + t, y0, y1, r, g, b);  /* left   */
        draw_vline(fb, fb_w, fb_h, x1 - t, y0, y1, r, g, b);  /* right  */
    }
}

/* Draw a filled rectangle (used for label background) */
static void fill_rect(uint8_t *fb, int fb_w, int fb_h,
                       int x0, int y0, int x1, int y1,
                       uint8_t r, uint8_t g, uint8_t b) {
    if (x0 > x1) { int t = x0; x0 = x1; x1 = t; }
    if (y0 > y1) { int t = y0; y0 = y1; y1 = t; }
    for (int y = y0; y <= y1; y++)
        draw_hline(fb, fb_w, fb_h, x0, x1, y, r, g, b);
}

/* -----------------------------------------------------------------------
 * Text rendering (5x7 bitmap font at FONT_SCALE)
 * ----------------------------------------------------------------------- */

/* Draw a single character at (px, py) with FONT_SCALE enlargement */
static void draw_char(uint8_t *fb, int fb_w, int fb_h,
                       int px, int py, int glyph_idx,
                       uint8_t r, uint8_t g, uint8_t b) {
    if (glyph_idx < 0 || glyph_idx >= GLYPH_COUNT)
        return;

    const uint8_t *glyph = font_5x7[glyph_idx];

    for (int row = 0; row < FONT_H; row++) {
        uint8_t bits = glyph[row];
        for (int col = 0; col < FONT_W; col++) {
            /* Bit 4 = leftmost pixel */
            if (bits & (1 << (FONT_W - 1 - col))) {
                /* Fill a FONT_SCALE x FONT_SCALE block */
                for (int sy = 0; sy < FONT_SCALE; sy++) {
                    for (int sx = 0; sx < FONT_SCALE; sx++) {
                        put_pixel(fb, fb_w, fb_h,
                                  px + col * FONT_SCALE + sx,
                                  py + row * FONT_SCALE + sy,
                                  r, g, b);
                    }
                }
            }
        }
    }
}

/* Compute rendered string width in pixels */
static int string_width(const char *s) {
    int len = 0;
    while (*s) {
        if (char_to_glyph(*s) >= 0)
            len++;
        s++;
    }
    if (len == 0) return 0;
    return len * CHAR_W + (len - 1) * CHAR_GAP;
}

/* Draw a string at (px, py). Unsupported chars are skipped. */
static void draw_string(uint8_t *fb, int fb_w, int fb_h,
                         int px, int py, const char *s,
                         uint8_t r, uint8_t g, uint8_t b) {
    int cx = px;
    while (*s) {
        int gi = char_to_glyph(*s);
        if (gi >= 0) {
            draw_char(fb, fb_w, fb_h, cx, py, gi, r, g, b);
            cx += CHAR_W + CHAR_GAP;
        }
        s++;
    }
}

/* -----------------------------------------------------------------------
 * Display confidence threshold
 *
 * Detections below this threshold are hidden in CAM overlay to reduce
 * visual noise. The inference-level BOX_THRESH (0.65) in postprocess.h
 * is preserved so HUD IPC still receives detections at the F1-optimal
 * operating point for GPS fusion logic in hud_live.py.
 * ----------------------------------------------------------------------- */

#define DISPLAY_CONF_THRESH  0.50f

/* -----------------------------------------------------------------------
 * Confidence -> color mapping (only two tiers after display threshold)
 * ----------------------------------------------------------------------- */

static void confidence_color(float conf, uint8_t *r, uint8_t *g, uint8_t *b) {
    if (conf >= 0.70f) {
        /* Green: high confidence */
        *r = 0;   *g = 255; *b = 0;
    } else {
        /* Yellow: medium confidence (>= DISPLAY_CONF_THRESH) */
        *r = 255; *g = 255; *b = 0;
    }
}

/* -----------------------------------------------------------------------
 * Public API
 * ----------------------------------------------------------------------- */

void overlay_draw_detections(uint8_t *fb, int fb_w, int fb_h,
                             const detect_result_group_t *dets) {
    if (!fb || !dets || dets->count <= 0)
        return;

    /*
     * Skip if the result is older than STALE_THRESHOLD_MS. Prevents box
     * "ghosting" -- the camera frame has moved on but inference hasn't
     * produced a new result yet, so the old box no longer aligns with
     * what's visible. Better to drop boxes briefly than mislead the user.
     */
    if (dets->last_update_ms > 0) {
        int64_t now_ms = time_us() / 1000;
        if (now_ms - dets->last_update_ms > STALE_THRESHOLD_MS)
            return;
    }

    int count = dets->count;
    if (count > OBJ_NUMB_MAX_SIZE)
        count = OBJ_NUMB_MAX_SIZE;

    for (int i = 0; i < count; i++) {
        const detect_result_t *d = &dets->results[i];

        /* Map bbox from NPU space (640x640) to display space (480x480) */
        int dx0 = map_x(d->box.left,   fb_w);
        int dy0 = map_y(d->box.top,    fb_h);
        int dx1 = map_x(d->box.right,  fb_w);
        int dy1 = map_y(d->box.bottom, fb_h);

        /* Skip degenerate boxes */
        if (dx1 - dx0 < 2 || dy1 - dy0 < 2)
            continue;

        /* Skip low-confidence detections (kept in IPC for GPS fusion) */
        if (d->prop < DISPLAY_CONF_THRESH)
            continue;

        /* Box color from confidence */
        uint8_t cr, cg, cb;
        confidence_color(d->prop, &cr, &cg, &cb);

        /* Draw bounding box outline */
        draw_rect(fb, fb_w, fb_h, dx0, dy0, dx1, dy1, BOX_THICK, cr, cg, cb);

        /* Build label: "XX YY%" (speed + confidence percentage) */
        char label[16];
        int speed = 0;
        if (d->class_id >= 0 && d->class_id < SIGN_SPEED_COUNT)
            speed = SIGN_SPEEDS[d->class_id];
        int conf_pct = (int)(d->prop * 100.0f + 0.5f);
        snprintf(label, sizeof(label), "%d %d%%", speed, conf_pct);

        /* Label position: above the box, left-aligned */
        int lbl_w = string_width(label);
        int lbl_h = CHAR_H;
        int lbl_x = dx0;
        int lbl_y = dy0 - LABEL_PAD - lbl_h - LABEL_PAD;

        /* If label would go above the screen, place it below the box */
        if (lbl_y < 0)
            lbl_y = dy1 + LABEL_PAD;
        if (lbl_y + lbl_h + LABEL_PAD >= fb_h)
            lbl_y = fb_h - lbl_h - LABEL_PAD - 1;
        if (lbl_y < LABEL_PAD)
            lbl_y = LABEL_PAD;

        /* Clamp label X to stay on screen */
        if (lbl_x + lbl_w + LABEL_PAD > fb_w)
            lbl_x = fb_w - lbl_w - LABEL_PAD;
        if (lbl_x < LABEL_PAD)
            lbl_x = LABEL_PAD;

        /* Draw label background (black) then text (white) */
        fill_rect(fb, fb_w, fb_h,
                  lbl_x - LABEL_PAD,
                  lbl_y - LABEL_PAD,
                  lbl_x + lbl_w + LABEL_PAD,
                  lbl_y + lbl_h + LABEL_PAD,
                  0, 0, 0);
        draw_string(fb, fb_w, fb_h, lbl_x, lbl_y, label, 255, 255, 255);
    }
}
