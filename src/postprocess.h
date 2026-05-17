/*
 * postprocess.h
 *
 * YOLOv5 post-processing for RKNN INT8 inference output.
 * Anchor box decoding, confidence filtering, and NMS.
 *
 * Target: Luckfox Pico Ultra (RV1106G3) -- pure C implementation
 * Reference: rknpu2 rknn_yolov5_demo (airockchip/rknn-toolkit2)
 */

#ifndef _POSTPROCESS_H_
#define _POSTPROCESS_H_

#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

/* --------------------------------------------------------------------------
 * Detection parameters
 * -------------------------------------------------------------------------- */

#define OBJ_NAME_MAX_SIZE   32
#define OBJ_NUMB_MAX_SIZE   64      /* Max detections per frame          */

/*
 * OBJ_CLASS_NUM: number of detection classes.
 * Override at compile time with -DOBJ_CLASS_NUM=80 for COCO testing.
 * Default: 11 (universal speed signs covering AU + CN)
 */
#ifndef OBJ_CLASS_NUM
#define OBJ_CLASS_NUM       11
#endif

#define NMS_THRESH          0.45f   /* IoU threshold for NMS             */
#define BOX_THRESH          0.40f   /* Object confidence threshold (conservative) */
#define PROP_BOX_SIZE       (5 + OBJ_CLASS_NUM)  /* 4 coords + 1 obj + classes */

/*
 * For mutually exclusive sign classes, suppress overlapping boxes across
 * different classes so one physical sign does not appear as nested boxes.
 * Keep class-aware NMS for generic multi-object datasets.
 */
#ifndef NMS_CLASS_AGNOSTIC
#if OBJ_CLASS_NUM == 11
#define NMS_CLASS_AGNOSTIC 1
#else
#define NMS_CLASS_AGNOSTIC 0
#endif
#endif

/* YOLOv5 has 3 detection heads with 3 anchors each */
#define NUM_ANCHORS         3
#define NUM_HEADS           3

/* YOLOv5 strides -- grid sizes are computed at runtime from model dimensions */
#define STRIDE_0            8
#define STRIDE_1            16
#define STRIDE_2            32

/* --------------------------------------------------------------------------
 * Data structures
 * -------------------------------------------------------------------------- */

typedef struct _box_rect_t {
    int left;
    int right;
    int top;
    int bottom;
} box_rect_t;

typedef struct _detect_result_t {
    char     name[OBJ_NAME_MAX_SIZE];
    box_rect_t box;
    float    prop;      /* Confidence score (0.0 ~ 1.0) */
    int      class_id;
} detect_result_t;

typedef struct _detect_result_group_t {
    int              id;
    int              count;
    detect_result_t  results[OBJ_NUMB_MAX_SIZE];
} detect_result_group_t;

/* --------------------------------------------------------------------------
 * API
 * -------------------------------------------------------------------------- */

/*
 * post_process - Decode YOLOv5 INT8 outputs and apply NMS.
 *
 * @input0:          Pointer to output tensor 0 (stride 8,  grid 40x40), INT8
 * @input1:          Pointer to output tensor 1 (stride 16, grid 20x20), INT8
 * @input2:          Pointer to output tensor 2 (stride 32, grid 10x10), INT8
 * @model_in_h:      Model input height (320)
 * @model_in_w:      Model input width  (320)
 * @conf_threshold:   Confidence threshold for filtering
 * @nms_threshold:    IoU threshold for NMS
 * @scale_w:          Horizontal scale factor (model_w / original_w)
 * @scale_h:          Vertical scale factor   (model_h / original_h)
 * @qnt_zps:          Zero-point array for each output tensor [3]
 * @qnt_scales:       Scale array for each output tensor [3]
 * @group:            Output detection results
 *
 * Returns 0 on success, negative on error.
 */
int post_process(int8_t *input0, int8_t *input1, int8_t *input2,
                 int model_in_h, int model_in_w,
                 float conf_threshold, float nms_threshold,
                 float scale_w, float scale_h,
                 int32_t *qnt_zps, float *qnt_scales,
                 detect_result_group_t *group);

/*
 * get_class_name - Return the label string for a given class index.
 */
const char *get_class_name(int class_id);

#ifdef __cplusplus
}
#endif

#endif /* _POSTPROCESS_H_ */
