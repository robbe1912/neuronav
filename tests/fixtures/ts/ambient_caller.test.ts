//- @go defines func
//- @go calls @wrapper
import { wrapper } from "./ambient_impl";

export function go(): string { return wrapper("x"); }
