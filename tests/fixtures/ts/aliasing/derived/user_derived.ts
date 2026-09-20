//- @auDerived defines func
//- @auDerived calls @dfunc
//- @auDerived calls @hfunc
import { dfunc } from "@/dwidget";
import { hfunc } from "@base/dhelp";

export function auDerived(): number { return dfunc() + hfunc(); }

