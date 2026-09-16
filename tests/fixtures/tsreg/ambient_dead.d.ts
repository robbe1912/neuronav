// a .d.ts path can carry real callables (compiler-baseline shape):
// the suffix must not assume zero surface — dead share applies
export function amb_dead_one(p: number[] = []): void { }
export function amb_dead_two(rParam: string): void { }
