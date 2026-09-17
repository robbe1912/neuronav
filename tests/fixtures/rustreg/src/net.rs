//! Closed pub-mod leaf: every pub fn here is a closure root via
//! lib.rs's `pub mod net;`.

pub fn send(packet: &str) -> usize {
    packet.len()
}
