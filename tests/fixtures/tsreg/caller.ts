import { ts_used_helper } from "./deadshare";

export function ts_caller(x: number): number {
  return ts_used_helper(x);
}
