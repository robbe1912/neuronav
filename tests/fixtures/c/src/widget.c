/* Fixture: the impl paired to widget.h (stem match) — definitions the
   header only declares, plus a file-private static. */
#include <stdio.h>
#include "widget.h"

struct widget {
    int x;
    int y;
    int state;
};

static void set_active(widget_t *w) {
    w->state = W_ACTIVE;
}

void widget_init(widget_t *w) {
    w->x = 0;
    w->y = 0;
    w->state = W_IDLE;
}

void widget_draw(const widget_t *w) {
    if (w->state == W_ACTIVE) {
        printf("[%d,%d]\n", w->x, w->y);
    }
}

void register_tick(void (*fn)(int)) {
    (void)fn;
}
