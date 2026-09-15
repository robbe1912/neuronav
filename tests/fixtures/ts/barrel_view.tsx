//- @view defines func
//- @view calls @alpha
//- @view calls @beta
import { alpha } from "./barrel_index";
import { beta } from "./barrel_next";
import { gone } from "./dead_helpers";

export function view(): number { return alpha() + beta(); }
