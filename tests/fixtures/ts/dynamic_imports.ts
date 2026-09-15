//- @preload defines func
//- include ts/lazy/mod.ts
export function preload(): void {
  const m = import("./lazy/mod");
}

export function cjs(): void {
  const n = require("./lazy/mod");
}

export function dyn(name: string): void {
  const o = import(name);
}
