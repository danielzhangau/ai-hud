/* hud_ipc.c -- Definitions for IPC data shared across translation units. */

#include "hud_ipc.h"

/* Speed limit class-to-value mapping (11 classes, AU + CN combined).
 * Declared extern in hud_ipc.h, single definition here. */
const int SIGN_SPEEDS[] = {20, 30, 40, 50, 60, 70, 80, 90, 100, 110, 120};
