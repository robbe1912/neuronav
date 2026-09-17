import { useShape } from './arrow_fns.js';
import { useBoth } from './barrel_view.js';
import { run } from './interop.js';
import { go } from './dynamic_imports.js';
import { useAlias } from './aliasing/user.js';
import { drive } from './super_calls.js';
import { useCounter } from './dead_helpers.js';
import { useTs } from './mixed/caller.js';
import { useJs } from './mixed/tscaller.ts';

export function libmain() {
  useShape(); useBoth(); run(); go(); useAlias(); drive(); useCounter(); useTs(); useJs();
}
