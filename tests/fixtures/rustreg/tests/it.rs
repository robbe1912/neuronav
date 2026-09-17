//! Integration test (anubis tests shape, issue #284): reaches the lib
//! through the crate name — name-level wiring by design, no
//! cross-crate static edges (documented non-goal).

#[test]
fn sends_packet() {
    let n = rustreg::net::send("hi");
    assert!(n > 0);
}

#[test]
fn serves() {
    let _ = rustreg::api::serve(8080);
}
