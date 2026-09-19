/* Fixture: the program entry — system include (skipped), own header,
   definition header, static helper, callback registration. */
#include <stdio.h>
#include "widget.h"
#include "util.h"

static void on_tick(int tick) {
    printf("tick %d\n", tick);
}

static int helper(int a) {
    return a + 1;
}

int main(int argc, char **argv) {
    widget_t w;
    widget_init(&w);
    widget_draw(&w);
    (void)argc;
    (void)argv;
    register_tick(&on_tick);
    return util_sum(helper(1), 2);
}
