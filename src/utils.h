/* utils.h -- Shared utility macros/functions for the ai-hud C binary.
 *
 * Consolidates common patterns duplicated across source files:
 *   - Clamping (integer, float, byte-range)
 *   - Microsecond timestamps
 *   - Framebuffer pixel format constants
 */

#ifndef UTILS_H
#define UTILS_H

#include <stdint.h>
#include <sys/time.h>

/* -----------------------------------------------------------------------
 * Clamping macros
 * ----------------------------------------------------------------------- */

/* Generic clamp: works for int, float, int64_t, etc.
 * Uses temporary variables to avoid double-evaluation of arguments. */
#define CLAMP(val, lo, hi) ({          \
    __typeof__(val) _v = (val);        \
    __typeof__(lo) _lo = (lo);         \
    __typeof__(hi) _hi = (hi);         \
    _v < _lo ? _lo : (_v > _hi ? _hi : _v); \
})

/* Clamp an integer to [0, 255] (byte range). */
#define CLAMP8(v) ({                   \
    int _v8 = (v);                     \
    _v8 < 0 ? 0 : (_v8 > 255 ? 255 : _v8); \
})

/* -----------------------------------------------------------------------
 * Microsecond timestamp
 * ----------------------------------------------------------------------- */

static inline int64_t time_us(void) {
    struct timeval tv;
    gettimeofday(&tv, NULL);
    return (int64_t)tv.tv_sec * 1000000 + tv.tv_usec;
}

/* -----------------------------------------------------------------------
 * Framebuffer pixel format (XRGB8888)
 * ----------------------------------------------------------------------- */

#define FB_BPP  4   /* Bytes per pixel: XRGB8888 */

#endif /* UTILS_H */
