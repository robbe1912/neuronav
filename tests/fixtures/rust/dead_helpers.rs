// not declared in lib.rs at all: nothing here is reachable, pub or not.
fn orphan_one() -> u32 {
    11
}

fn orphan_two() -> u32 {
    22
}

pub fn orphan_pub() -> u32 {
    33
}
