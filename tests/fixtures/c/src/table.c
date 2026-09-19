/* Fixture: TU-scope function-pointer table — the first handler stays
   alive through the bare-name entry (no container: liveness lands via
   referenced_names); the second handler has no such ref and stays
   likely-dead. */
typedef void (*handler_fn)(void);

static void handler_a(void) {
}

static void second_handler(void) {
}

static const handler_fn TABLE[] = {
    handler_a,
};

int table_run(int i) {
    return TABLE[i] != 0;
}
