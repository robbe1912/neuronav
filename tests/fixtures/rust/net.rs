// module reached via `pub mod net;` — both pub fns are exported roots.
//- @send defines func
//- @recv defines func
pub fn send(seq: u32) -> u32 {
    seq + 1
}

pub fn recv(seq: u32) -> u32 {
    seq - 1
}

fn internal_dead2() -> u32 {
    7
}
