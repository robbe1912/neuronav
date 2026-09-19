/* Fixture: dead material — no main, no callers, no callback refs. */
#include "widget.h"

int never_called(widget_t *w) {
    return w->x + 1;
}

static float also_dead(float f) {
    return f * 2.0f;
}
