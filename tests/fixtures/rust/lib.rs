// crate root: pub-mod closure roots the exported API; private fn is dead.
//- @api_exported defines func
//- @internal_dead dead
pub fn api_exported() -> u32 {
    crate::net::send(1) + 2
}

pub mod net;
pub mod methods;
pub mod traits;
mod helpers;

pub use net::send as transmit;

fn internal_dead() -> u32 {
    42
}
