// fixture 11: module tree of the bin target bin/cli.rs — resolved
// through the bin's own crate root, not the fixture-root lib.rs.
pub fn shake() -> u32 {
    7
}

pub fn steady(word: &str) -> usize {
    word.len()
}
