/* Fixture: definition header — static inline fns live IN the header
   (no paired .c): call targets land on the header itself. */
#ifndef UTIL_H
#define UTIL_H

static inline int util_sum(int a, int b) {
    return a + b;
}

static inline int util_unused(void) {
    return 42;
}

#endif
