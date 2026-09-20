//- @auOverride defines func
//- @auOverride calls @ofunc
//- @auOverride calls @bfunc
import { ofunc } from "@/owidget";
import { bfunc } from "@base/bhelp";

export function auOverride(): number { return ofunc() + bfunc(); }

