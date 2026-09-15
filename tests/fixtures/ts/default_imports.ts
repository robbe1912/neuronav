//- @run defines func
//- @run calls @Local
import Local from "./anon_default";
import App from "./named_default";

export function run(): number { return Local(); }
