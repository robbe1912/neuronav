// macros: call-sites recorded (name_literals), templates never expanded —
// a fn referenced only from a template body stays dead (review tier: the
// template text feeds the mention floor).
//- @macro_user defines func
//- @template_only_fn dead
macro_rules! helper_macro {
    ($v:expr) => {
        $v + 1
    };
}

macro_rules! call_named {
    () => {
        template_only_fn()
    };
}

fn template_only_fn() -> u32 {
    1
}

fn macro_user(x: u32) -> u32 {
    helper_macro!(x)
}

fn never_called_via_macro() -> u32 {
    2
}
