//! Hermetic rust regression crate (issue #284): mirrors the anubis
//! daemon-rs shape — lib.rs pub-mod barrel + re-export, a bin target
//! that is its OWN crate root, and an integration test wired through
//! the crate name (name-level liveness by design).

pub mod api;
pub mod net;

pub use api::serve as launch;
