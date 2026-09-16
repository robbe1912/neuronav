// test attributes are roots: #[test], #[tokio::test], and #[cfg(test)]
// mods mark only their #[test]-attributed fns — cfg-mod helpers stay dead.
//- @checks_a defines func
//- @checks_a calls @unit_under_test
#[test]
fn checks_a() {
    let v = unit_under_test(2);
    assert_eq!(v, 4);
    let m = crate::macros::macro_user(1);
    assert_eq!(m, 2);
}

#[tokio::test]
async fn checks_b() {
    let v = unit_under_test(3);
    assert_eq!(v, 6);
}

#[tokio::test]
#[ignore]
async fn ignored_stack_entry() {
    let v = unit_under_test(9);
    assert_eq!(v, 18);
}

fn unit_under_test(x: u32) -> u32 {
    x * 2
}

#[cfg(test)]
mod inner_tests {
    fn deep_helper() -> u32 {
        5
    }

    #[test]
    fn deep_test() {
        assert_eq!(deep_helper(), 5);
    }

    fn dead_in_test_mod() -> u32 {
        6
    }
}
