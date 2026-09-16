// `mod helpers;` (no pub) in lib.rs: pub fn here is NOT exported — only
// the fn actually called in-crate survives.
pub fn helper_alive(x: u32) -> u32 {
    x * 2
}

pub fn helper_pub_dead() -> u32 {
    99
}
