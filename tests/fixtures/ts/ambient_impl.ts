//- @wrapper defines func
//- @wrapper calls @shipped
import { shipped } from "./ambient.d.ts";

export function wrapper(s: string): string { return shipped(s); }
