// trait with required sig + default method; impl block collapses onto the
// first declaration line (overload law).
//- class Adapter
//- alias Handler = trait
//- @handle defines func
//- @label defines func
//- @handle calls @label
pub trait Handler {
    fn handle(&self) -> u32;

    fn label(&self) -> String {
        String::from("handler")
    }
}

pub struct Adapter {
    pub seq: u32,
}

impl Handler for Adapter {
    fn handle(&self) -> u32 {
        let name = self.label();
        name.len() as u32 + self.seq
    }
}
