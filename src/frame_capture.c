/*
 * frame_capture.c -- On-device frame capture for model iteration.
 *
 * Thread-safety: Only called from the single inference thread,
 * no synchronization needed.
 */

#include "frame_capture.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <limits.h>
#include <sys/stat.h>
#include <sys/time.h>
#include <dirent.h>
#include <unistd.h>
#include <time.h>
#include <errno.h>

/* Trigger type strings for metadata */
static const char *TRIG_NAMES[] = {"detect", "periodic", "manual"};

/* --------------------------------------------------------------------------
 * Module state (single instance, inference thread only)
 * -------------------------------------------------------------------------- */

static struct {
    char dir[256];
    int max_frames;
    int frame_count;
    int next_id;
    int initialized;

    /* Timing for trigger logic */
    int64_t last_detect_save_us;
    int64_t last_periodic_save_us;
} g_cap;

/* --------------------------------------------------------------------------
 * Internal helpers
 * -------------------------------------------------------------------------- */

static int64_t cap_time_us(void)
{
    struct timeval tv;
    gettimeofday(&tv, NULL);
    return (int64_t)tv.tv_sec * 1000000 + tv.tv_usec;
}

/*
 * Read GPS coordinates from Python IPC file.
 * Format: "lat=XX.XXXXXX\nlon=YY.YYYYYY\n"
 */
static void read_gps(float *lat, float *lon)
{
    *lat = 0.0f;
    *lon = 0.0f;

    FILE *fp = fopen(CAPTURE_GPS_IPC_FILE, "r");
    if (!fp)
        return;

    char line[64];
    while (fgets(line, sizeof(line), fp)) {
        if (strncmp(line, "lat=", 4) == 0)
            *lat = strtof(line + 4, NULL);
        else if (strncmp(line, "lon=", 4) == 0)
            *lon = strtof(line + 4, NULL);
    }
    fclose(fp);
}

/*
 * Scan capture directory: count existing frames, find max ID.
 */
static void scan_dir(void)
{
    DIR *d = opendir(g_cap.dir);
    if (!d)
        return;

    g_cap.frame_count = 0;
    g_cap.next_id = 0;

    struct dirent *ent;
    while ((ent = readdir(d)) != NULL) {
        if (!strstr(ent->d_name, ".nv12"))
            continue;
        g_cap.frame_count++;
        int id = 0;
        if (sscanf(ent->d_name, "%d_", &id) == 1) {
            if (id >= g_cap.next_id)
                g_cap.next_id = id + 1;
        }
    }
    closedir(d);
}

/*
 * Delete the oldest frame (lowest ID) to make room for new captures.
 */
static void delete_oldest(void)
{
    DIR *d = opendir(g_cap.dir);
    if (!d)
        return;

    char oldest_name[256] = "";
    int oldest_id = INT_MAX;

    struct dirent *ent;
    while ((ent = readdir(d)) != NULL) {
        if (!strstr(ent->d_name, ".nv12"))
            continue;
        int id = 0;
        if (sscanf(ent->d_name, "%d_", &id) == 1 && id < oldest_id) {
            oldest_id = id;
            snprintf(oldest_name, sizeof(oldest_name), "%s", ent->d_name);
        }
    }
    closedir(d);

    if (!oldest_name[0])
        return;

    char path[512];

    /* Delete .nv12 file */
    snprintf(path, sizeof(path), "%s/%s", g_cap.dir, oldest_name);
    unlink(path);

    /* Delete corresponding .json sidecar */
    char json_name[256];
    snprintf(json_name, sizeof(json_name), "%s", oldest_name);
    char *ext = strstr(json_name, ".nv12");
    if (ext) {
        strcpy(ext, ".json");
        snprintf(path, sizeof(path), "%s/%s", g_cap.dir, json_name);
        unlink(path);
    }

    g_cap.frame_count--;
}

/*
 * Write metadata JSON sidecar for a captured frame.
 */
static void write_metadata(const char *json_path, long timestamp,
                           int width, int height, float speed_kmh,
                           float lat, float lon, int trigger,
                           const detect_result_group_t *results)
{
    FILE *fp = fopen(json_path, "w");
    if (!fp)
        return;

    fprintf(fp, "{\n");
    fprintf(fp, "  \"timestamp\": %ld,\n", timestamp);
    fprintf(fp, "  \"width\": %d,\n", width);
    fprintf(fp, "  \"height\": %d,\n", height);
    fprintf(fp, "  \"speed_kmh\": %.1f,\n", speed_kmh);
    fprintf(fp, "  \"lat\": %.6f,\n", lat);
    fprintf(fp, "  \"lon\": %.6f,\n", lon);
    fprintf(fp, "  \"trigger\": \"%s\",\n", TRIG_NAMES[trigger]);
    fprintf(fp, "  \"detections\": [");

    if (results) {
        for (int i = 0; i < results->count; i++) {
            const detect_result_t *r = &results->results[i];
            fprintf(fp, "%s\n    {\"class\": \"%s\", \"class_id\": %d, "
                    "\"confidence\": %.3f, "
                    "\"box\": [%d, %d, %d, %d]}",
                    (i > 0) ? "," : "",
                    r->name, r->class_id, r->prop,
                    r->box.left, r->box.top, r->box.right, r->box.bottom);
        }
    }

    fprintf(fp, "\n  ]\n}\n");
    fclose(fp);
}

/* --------------------------------------------------------------------------
 * Public API
 * -------------------------------------------------------------------------- */

int capture_init(const char *output_dir, int max_frames)
{
    memset(&g_cap, 0, sizeof(g_cap));

    snprintf(g_cap.dir, sizeof(g_cap.dir), "%s",
             output_dir ? output_dir : CAPTURE_DIR_DEFAULT);
    g_cap.max_frames = (max_frames > 0) ? max_frames : CAPTURE_MAX_FRAMES_DEFAULT;

    /* Create directory (ok if already exists) */
    mkdir(g_cap.dir, 0755);

    scan_dir();
    g_cap.initialized = 1;

    printf("[CAPTURE] Initialized: dir=%s, existing=%d, max=%d\n",
           g_cap.dir, g_cap.frame_count, g_cap.max_frames);
    return 0;
}

int capture_is_enabled(void)
{
    FILE *fp = fopen(CAPTURE_ENABLE_IPC_FILE, "r");
    if (!fp)
        return 0;  /* Default: disabled */
    int val = 0;
    if (fscanf(fp, "%d", &val) != 1)
        val = 0;
    fclose(fp);
    return val != 0;
}

int capture_check_and_save(const uint8_t *nv12_data, int width, int height,
                           const detect_result_group_t *results,
                           float speed_kmh)
{
    if (!g_cap.initialized || !nv12_data)
        return 0;

    if (!capture_is_enabled())
        return 0;

    int64_t now_us = cap_time_us();
    int trigger = -1;

    /* --- Detection trigger: save when sign detected with sufficient confidence --- */
    if (results && results->count > 0) {
        float top_conf = 0.0f;
        for (int i = 0; i < results->count; i++) {
            if (results->results[i].prop > top_conf)
                top_conf = results->results[i].prop;
        }

        if (top_conf >= CAPTURE_MIN_CONF) {
            int64_t elapsed = now_us - g_cap.last_detect_save_us;
            if (elapsed >= (int64_t)CAPTURE_DETECT_COOLDOWN_MS * 1000) {
                trigger = CAPTURE_TRIG_DETECT;
                g_cap.last_detect_save_us = now_us;
            }
        }
    }

    /* --- Periodic trigger: sample road scenes while driving --- */
    if (trigger < 0 && speed_kmh > CAPTURE_MIN_SPEED_KMH) {
        int64_t elapsed = now_us - g_cap.last_periodic_save_us;
        if (elapsed >= (int64_t)CAPTURE_PERIODIC_SEC * 1000000) {
            trigger = CAPTURE_TRIG_PERIODIC;
            g_cap.last_periodic_save_us = now_us;
        }
    }

    if (trigger < 0)
        return 0;

    /* Enforce storage limit (FIFO) */
    while (g_cap.frame_count >= g_cap.max_frames)
        delete_oldest();

    /* Generate file paths */
    long ts = (long)time(NULL);
    int id = g_cap.next_id++;
    char base[512];
    snprintf(base, sizeof(base), "%s/%06d_%ld", g_cap.dir, id, ts);

    /* Save NV12 raw frame */
    char nv12_path[520];
    snprintf(nv12_path, sizeof(nv12_path), "%s.nv12", base);

    int nv12_size = width * height * 3 / 2;
    FILE *fp = fopen(nv12_path, "wb");
    if (!fp) {
        printf("[CAPTURE] Failed to write %s: %s\n", nv12_path, strerror(errno));
        return -1;
    }
    fwrite(nv12_data, 1, nv12_size, fp);
    fclose(fp);

    /* Read GPS coords and save metadata */
    float lat, lon;
    read_gps(&lat, &lon);

    char json_path[520];
    snprintf(json_path, sizeof(json_path), "%s.json", base);
    write_metadata(json_path, ts, width, height, speed_kmh,
                   lat, lon, trigger, results);

    g_cap.frame_count++;

    printf("[CAPTURE] Saved #%d (%s, %dx%d, %d dets, speed=%.0f km/h)\n",
           id, TRIG_NAMES[trigger], width, height,
           results ? results->count : 0, speed_kmh);

    return 1;
}

void capture_release(void)
{
    if (g_cap.initialized)
        printf("[CAPTURE] Released (%d frames stored in %s)\n",
               g_cap.frame_count, g_cap.dir);
    g_cap.initialized = 0;
}
