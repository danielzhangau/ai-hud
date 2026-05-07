/*
 * postprocess.c
 *
 * YOLOv5 INT8 post-processing: anchor box decoding, NMS, confidence filtering.
 *
 * Target: Luckfox Pico Ultra (RV1106G3)
 * Reference: rknpu2 rknn_yolov5_demo (airockchip/rknn-toolkit2)
 *
 * The RKNN model exports 3 output tensors in NHWC layout (RV1106):
 *   output0: [1, 40, 40, 3*(5+9)] = [1, 40, 40, 42]  stride 8
 *   output1: [1, 20, 20, 3*(5+9)] = [1, 20, 20, 42]  stride 16
 *   output2: [1, 10, 10, 3*(5+9)] = [1, 10, 10, 42]  stride 32
 *
 * Each anchor produces (5 + OBJ_CLASS_NUM) values:
 *   [tx, ty, tw, th, obj_conf, cls0, cls1, ..., cls8]
 * All values are INT8 with per-tensor affine quantization (zp, scale).
 */

#include "postprocess.h"

#include <math.h>
#include <string.h>
#include <stdio.h>
#include <stdlib.h>

/* --------------------------------------------------------------------------
 * Class labels -- Australian speed signs and speed cameras
 * -------------------------------------------------------------------------- */

static const char *class_names[OBJ_CLASS_NUM] = {
    "speed_sign_30",
    "speed_sign_40",
    "speed_sign_50",
    "speed_sign_60",
    "speed_sign_70",
    "speed_sign_80",
    "speed_sign_100",
    "speed_sign_110",
    "speed_camera"
};

const char *get_class_name(int class_id) {
    if (class_id < 0 || class_id >= OBJ_CLASS_NUM)
        return "unknown";
    return class_names[class_id];
}

/* --------------------------------------------------------------------------
 * YOLOv5 default anchors (pixel space, per-stride)
 *
 * Standard YOLOv5 anchors. If the model was trained with custom anchors,
 * update these values accordingly.
 * -------------------------------------------------------------------------- */

static const int anchors[NUM_HEADS][NUM_ANCHORS * 2] = {
    /* stride 8  (large feature map, small objects) */
    { 10, 13,  16, 30,  33, 23 },
    /* stride 16 (medium feature map) */
    { 30, 61,  62, 45,  59, 119 },
    /* stride 32 (small feature map, large objects) */
    { 116, 90,  156, 198,  373, 326 }
};

static const int strides[NUM_HEADS] = { 8, 16, 32 };

/* --------------------------------------------------------------------------
 * Quantization helpers
 * -------------------------------------------------------------------------- */

static inline float deqnt_affine_to_f32(int8_t qnt, int32_t zp, float scale) {
    return ((float)qnt - (float)zp) * scale;
}

/* --------------------------------------------------------------------------
 * Activation functions
 *
 * For INT8 quantized output, we compute sigmoid on the dequantized float.
 * We also provide an "unsigmoid" to convert a float threshold back to
 * the logit domain, so we can filter in quantized space for speed.
 * -------------------------------------------------------------------------- */

static inline float sigmoid(float x) {
    return 1.0f / (1.0f + expf(-x));
}

static inline float unsigmoid(float y) {
    return -logf(1.0f / y - 1.0f);
}

/*
 * Convert a float threshold to the INT8 quantized domain.
 * If sigmoid(deqnt(qnt)) > threshold, then qnt > qnt_threshold.
 * This allows early rejection without dequantization.
 */
static inline int8_t qnt_threshold(float threshold, int32_t zp, float scale) {
    float logit = unsigmoid(threshold);
    float qnt_f = logit / scale + (float)zp;
    /* Clamp to int8 range */
    if (qnt_f > 127.0f) return 127;
    if (qnt_f < -128.0f) return -128;
    return (int8_t)(qnt_f + 0.5f);
}

/* --------------------------------------------------------------------------
 * IoU (Intersection over Union) calculation
 * -------------------------------------------------------------------------- */

static float calculate_overlap(float x1_min, float y1_min, float x1_max, float y1_max,
                                float x2_min, float y2_min, float x2_max, float y2_max) {
    float inter_x_min = (x1_min > x2_min) ? x1_min : x2_min;
    float inter_y_min = (y1_min > y2_min) ? y1_min : y2_min;
    float inter_x_max = (x1_max < x2_max) ? x1_max : x2_max;
    float inter_y_max = (y1_max < y2_max) ? y1_max : y2_max;

    float inter_w = inter_x_max - inter_x_min;
    float inter_h = inter_y_max - inter_y_min;

    if (inter_w <= 0.0f || inter_h <= 0.0f)
        return 0.0f;

    float inter_area = inter_w * inter_h;
    float area1 = (x1_max - x1_min) * (y1_max - y1_min);
    float area2 = (x2_max - x2_min) * (y2_max - y2_min);
    float union_area = area1 + area2 - inter_area;

    if (union_area <= 0.0f)
        return 0.0f;

    return inter_area / union_area;
}

/* --------------------------------------------------------------------------
 * Internal accumulation buffer
 * -------------------------------------------------------------------------- */

/* Maximum raw detections before NMS (across all 3 heads) */
#define MAX_RAW_DETECTIONS 4096

typedef struct _raw_detection_t {
    float x1, y1, x2, y2;   /* bbox in model input coords */
    float score;              /* obj_conf * class_conf */
    int   class_id;
} raw_detection_t;

/* --------------------------------------------------------------------------
 * Quicksort (descending by score, in-place)
 * -------------------------------------------------------------------------- */

static void swap_detections(raw_detection_t *a, raw_detection_t *b) {
    raw_detection_t tmp = *a;
    *a = *b;
    *b = tmp;
}

static int partition(raw_detection_t *arr, int low, int high) {
    float pivot = arr[high].score;
    int i = low - 1;
    for (int j = low; j < high; j++) {
        if (arr[j].score >= pivot) {  /* descending order */
            i++;
            swap_detections(&arr[i], &arr[j]);
        }
    }
    swap_detections(&arr[i + 1], &arr[high]);
    return i + 1;
}

static void quicksort(raw_detection_t *arr, int low, int high) {
    if (low < high) {
        int pi = partition(arr, low, high);
        quicksort(arr, low, pi - 1);
        quicksort(arr, pi + 1, high);
    }
}

/* --------------------------------------------------------------------------
 * NMS (Non-Maximum Suppression) -- per-class
 * -------------------------------------------------------------------------- */

static int nms(raw_detection_t *dets, int count, float nms_threshold,
               int *keep, int max_keep) {
    /*
     * Simple greedy NMS:
     * 1. Sort by score (already sorted before calling)
     * 2. For each detection, suppress all later detections of the same
     *    class with IoU > threshold
     */
    int *suppressed = (int *)calloc(count, sizeof(int));
    if (!suppressed) return 0;

    int keep_count = 0;

    for (int i = 0; i < count && keep_count < max_keep; i++) {
        if (suppressed[i])
            continue;

        keep[keep_count++] = i;

        for (int j = i + 1; j < count; j++) {
            if (suppressed[j])
                continue;
            /* Only suppress same-class detections */
            if (dets[i].class_id != dets[j].class_id)
                continue;

            float iou = calculate_overlap(
                dets[i].x1, dets[i].y1, dets[i].x2, dets[i].y2,
                dets[j].x1, dets[j].y1, dets[j].x2, dets[j].y2);

            if (iou > nms_threshold)
                suppressed[j] = 1;
        }
    }

    free(suppressed);
    return keep_count;
}

/* --------------------------------------------------------------------------
 * Clamp utility
 * -------------------------------------------------------------------------- */

static inline float clamp_f(float val, float min_val, float max_val) {
    if (val < min_val) return min_val;
    if (val > max_val) return max_val;
    return val;
}

static inline int clamp_i(int val, int min_val, int max_val) {
    if (val < min_val) return min_val;
    if (val > max_val) return max_val;
    return val;
}

/* --------------------------------------------------------------------------
 * Process a single detection head
 *
 * RKNN RV1106 outputs in NHWC layout via RKNN_QUERY_NATIVE_NHWC_OUTPUT_ATTR:
 *   [1, grid_h, grid_w, NUM_ANCHORS * PROP_BOX_SIZE]
 *
 * For each grid cell (y, x), for each anchor a:
 *   offset = (y * grid_w + x) * (NUM_ANCHORS * PROP_BOX_SIZE) + a * PROP_BOX_SIZE
 *   data[offset + 0] = tx    (box center x offset)
 *   data[offset + 1] = ty    (box center y offset)
 *   data[offset + 2] = tw    (box width)
 *   data[offset + 3] = th    (box height)
 *   data[offset + 4] = obj   (objectness confidence)
 *   data[offset + 5..5+C-1] = class probabilities
 *
 * Decoding (YOLOv5):
 *   bx = (sigmoid(tx) * 2 - 0.5 + grid_x) * stride
 *   by = (sigmoid(ty) * 2 - 0.5 + grid_y) * stride
 *   bw = (sigmoid(tw) * 2)^2 * anchor_w
 *   bh = (sigmoid(th) * 2)^2 * anchor_h
 * -------------------------------------------------------------------------- */

static int process_head(int8_t *input, const int *anchor, int grid_h, int grid_w,
                        int stride, int32_t zp, float scale,
                        float conf_threshold,
                        raw_detection_t *out, int max_out) {
    int count = 0;

    /*
     * Pre-compute quantized threshold for objectness.
     * If the INT8 value is below this, sigmoid(deqnt(val)) < conf_threshold,
     * so we can skip without full dequantization.
     */
    int8_t qnt_obj_thresh = qnt_threshold(conf_threshold, zp, scale);

    int channel_size = NUM_ANCHORS * PROP_BOX_SIZE;

    for (int y = 0; y < grid_h; y++) {
        for (int x = 0; x < grid_w; x++) {
            int base = (y * grid_w + x) * channel_size;

            for (int a = 0; a < NUM_ANCHORS; a++) {
                int offset = base + a * PROP_BOX_SIZE;

                /* Quick objectness check in quantized space */
                int8_t obj_qnt = input[offset + 4];
                if (obj_qnt < qnt_obj_thresh)
                    continue;

                /* Dequantize objectness and apply sigmoid */
                float obj_conf = sigmoid(deqnt_affine_to_f32(obj_qnt, zp, scale));
                if (obj_conf < conf_threshold)
                    continue;

                /* Find best class */
                int best_cls = 0;
                float best_cls_score = -1.0f;
                for (int c = 0; c < OBJ_CLASS_NUM; c++) {
                    float cls_val = sigmoid(
                        deqnt_affine_to_f32(input[offset + 5 + c], zp, scale));
                    if (cls_val > best_cls_score) {
                        best_cls_score = cls_val;
                        best_cls = c;
                    }
                }

                float final_score = obj_conf * best_cls_score;
                if (final_score < conf_threshold)
                    continue;

                if (count >= max_out)
                    return count;

                /* Decode bounding box (YOLOv5 formula) */
                float tx = deqnt_affine_to_f32(input[offset + 0], zp, scale);
                float ty = deqnt_affine_to_f32(input[offset + 1], zp, scale);
                float tw = deqnt_affine_to_f32(input[offset + 2], zp, scale);
                float th = deqnt_affine_to_f32(input[offset + 3], zp, scale);

                float bx = (sigmoid(tx) * 2.0f - 0.5f + (float)x) * (float)stride;
                float by = (sigmoid(ty) * 2.0f - 0.5f + (float)y) * (float)stride;
                float bw_half = (sigmoid(tw) * 2.0f);
                bw_half = bw_half * bw_half * (float)anchor[a * 2 + 0] * 0.5f;
                float bh_half = (sigmoid(th) * 2.0f);
                bh_half = bh_half * bh_half * (float)anchor[a * 2 + 1] * 0.5f;

                out[count].x1 = bx - bw_half;
                out[count].y1 = by - bh_half;
                out[count].x2 = bx + bw_half;
                out[count].y2 = by + bh_half;
                out[count].score = final_score;
                out[count].class_id = best_cls;
                count++;
            }
        }
    }

    return count;
}

/* --------------------------------------------------------------------------
 * post_process -- Main entry point
 * -------------------------------------------------------------------------- */

int post_process(int8_t *input0, int8_t *input1, int8_t *input2,
                 int model_in_h, int model_in_w,
                 float conf_threshold, float nms_threshold,
                 float scale_w, float scale_h,
                 int32_t *qnt_zps, float *qnt_scales,
                 detect_result_group_t *group) {

    if (!input0 || !input1 || !input2 || !group)
        return -1;

    memset(group, 0, sizeof(detect_result_group_t));

    /* Allocate raw detection buffer */
    raw_detection_t *raw_dets = (raw_detection_t *)malloc(
        MAX_RAW_DETECTIONS * sizeof(raw_detection_t));
    if (!raw_dets) {
        printf("[ERROR] postprocess: failed to allocate detection buffer\n");
        return -1;
    }

    int total = 0;

    /* Process each detection head */
    /* Head 0: stride 8, grid 40x40 */
    total += process_head(input0, anchors[0], GRID_H_0, GRID_W_0,
                          strides[0], qnt_zps[0], qnt_scales[0],
                          conf_threshold,
                          &raw_dets[total], MAX_RAW_DETECTIONS - total);

    /* Head 1: stride 16, grid 20x20 */
    total += process_head(input1, anchors[1], GRID_H_1, GRID_W_1,
                          strides[1], qnt_zps[1], qnt_scales[1],
                          conf_threshold,
                          &raw_dets[total], MAX_RAW_DETECTIONS - total);

    /* Head 2: stride 32, grid 10x10 */
    total += process_head(input2, anchors[2], GRID_H_2, GRID_W_2,
                          strides[2], qnt_zps[2], qnt_scales[2],
                          conf_threshold,
                          &raw_dets[total], MAX_RAW_DETECTIONS - total);

    if (total == 0) {
        free(raw_dets);
        return 0;
    }

    /* Sort by score (descending) */
    quicksort(raw_dets, 0, total - 1);

    /* Apply NMS */
    int *keep_indices = (int *)malloc(total * sizeof(int));
    if (!keep_indices) {
        free(raw_dets);
        return -1;
    }

    int keep_count = nms(raw_dets, total, nms_threshold,
                         keep_indices, OBJ_NUMB_MAX_SIZE);

    /* Fill output group */
    group->count = keep_count;
    for (int i = 0; i < keep_count; i++) {
        raw_detection_t *d = &raw_dets[keep_indices[i]];
        detect_result_t *r = &group->results[i];

        /* Scale back to original image coordinates */
        float x1 = clamp_f(d->x1, 0.0f, (float)model_in_w) / scale_w;
        float y1 = clamp_f(d->y1, 0.0f, (float)model_in_h) / scale_h;
        float x2 = clamp_f(d->x2, 0.0f, (float)model_in_w) / scale_w;
        float y2 = clamp_f(d->y2, 0.0f, (float)model_in_h) / scale_h;

        r->box.left   = (int)(x1 + 0.5f);
        r->box.top    = (int)(y1 + 0.5f);
        r->box.right  = (int)(x2 + 0.5f);
        r->box.bottom = (int)(y2 + 0.5f);
        r->prop       = d->score;
        r->class_id   = d->class_id;

        const char *name = get_class_name(d->class_id);
        strncpy(r->name, name, OBJ_NAME_MAX_SIZE - 1);
        r->name[OBJ_NAME_MAX_SIZE - 1] = '\0';
    }

    free(keep_indices);
    free(raw_dets);

    return 0;
}
