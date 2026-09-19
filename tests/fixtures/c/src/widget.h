/* Fixture: pure-declaration header (funcs={} — declarations never
   match function_definition) + typedef + enum surface. */
#ifndef WIDGET_H
#define WIDGET_H

typedef struct widget widget_t;

enum widget_state { W_IDLE = 0, W_ACTIVE = 1 };

void widget_init(widget_t *w);
void widget_draw(const widget_t *w);
void register_tick(void (*fn)(int));

#endif
