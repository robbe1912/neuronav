// binary entry: fn main roots the file; qualified paths, method calls,
// re-export alias, dyn dispatch, macro call-site recording.
//- @main defines func
//- @main calls @compute
use crate::methods::Server;
use crate::net;
use crate::transmit;

fn main() -> std::io::Result<()> {
    net::recv(42);
    transmit(5);
    crate::helpers::helper_alive(3);
    let srv: Server = Server::new(String::from("a"));
    srv.compute(1);
    let h: Box<dyn Handler> = Box::new(AdapterHandler::new());
    h.handle();
    h.label();
    helper_macro!(2);
    Ok(())
}

fn unused_local(x: u32) -> u32 {
    x + 1
}

fn _shelved_helper(x: u32) -> u32 {
    x + 2
}
