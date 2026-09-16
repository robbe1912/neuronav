// struct + inherent impl: members, ret types, self-method edges.
//- class Server
//- member name:String
//- member seq:u32
//- @new defines func
//- @new ret Server
//- @compute calls @internal_step
pub struct Server {
    pub name: String,
    seq: u32,
}

impl Server {
    pub fn new(name: String) -> Server {
        Server { name, seq: 0 }
    }

    pub fn compute(&self, x: u32) -> u32 {
        let boosted = self.internal_step(x);
        boosted * 2
    }

    fn internal_step(&self, x: u32) -> u32 {
        x + self.seq
    }
}
//- alias Server = struct
