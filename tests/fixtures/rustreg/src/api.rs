//! Core module: one `use crate::net::send as deliver` alias (in-crate
//! rebind), two pub fns (closure roots), two dead fns — dead share 0.5
//! so this file carries the dead-file flag while the wiring-only
//! lib.rs barrel must stay out of it.

use crate::net::send as deliver;

pub fn serve(port: u16) -> String {
    let n = deliver("boot");
    format!("up:{port}:{n}")
}

pub fn auth_gate(token: &str) -> bool {
    !token.is_empty()
}

fn courier() {
    let _ = deliver("noop");
}

fn orphan_dead() {}
