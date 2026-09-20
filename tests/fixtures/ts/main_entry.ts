//- @main defines func
import { view } from "./barrel_view";
import { run } from "./default_imports";
import { au } from "./aliasing/user";
import { auDerived } from "./aliasing/derived/user_derived";
import { auOverride } from "./aliasing/override/user_override";
import { usegen } from "./generics_calls";
import { drive } from "./super_calls";
import { render } from "./jsx_composition";
import { user } from "./arrow_methods";
import { caller } from "./overloads";
import { Counter } from "./dead_helpers";

export function main(): void {
  view(); run(); au(); usegen(); drive(); render(); user(); caller();
  auDerived(); auOverride();
  const c: Counter = new Counter();
  c.bump();
}
