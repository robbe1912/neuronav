//- @main defines func
import { view } from "./barrel_view";
import { run } from "./default_imports";
import { au } from "./aliasing/user";
import { usegen } from "./generics_calls";
import { drive } from "./super_calls";
import { render } from "./jsx_composition";
import { user } from "./arrow_methods";
import { caller } from "./overloads";
import { Counter } from "./dead_helpers";

export function main(): void {
  view(); run(); au(); usegen(); drive(); render(); user(); caller();
  const c: Counter = new Counter();
  c.bump();
}
