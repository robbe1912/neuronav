//! Cargo bin target (issue #284): its OWN crate root — crate:: paths
//! anchor at src/bin/rt/, never at the lib's src/.
mod helper;

use crate::helper::shake as jolt;

fn main() {
    let n = jolt();
    let next = helper::steady(n);
    let _again = crate::helper::shake();
    println!("{next}");
}
