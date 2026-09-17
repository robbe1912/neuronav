// fixture 11 (issue #284): a cargo bin target directly under bin/ is
// its OWN crate root — crate:: paths and bare module heads anchor at
// bin/cli/, never at the sibling lib.rs tree at the fixture root.
mod helper;

use crate::helper::shake as jolt;

fn main() {
    let _n = jolt();
    helper::steady("boot");
    let _m = crate::helper::shake();
}

pub fn bin_orphan() {
    let _ = jolt();
}
