// pure-delegate twins for graph's dup filter (issue #364): fwdAlpha and
// fwdBeta store byte-identical bare-brace bodies (the extractor's stored
// form) and classify through the shared ts hook; richA/richB carry real
// logic and are the non-delegate control group
export function fwdAlpha(v) {
    return shared(v);
}

export function fwdBeta(v) {
    return shared(v);
}

export function richA(n) {
    const t = n * 2;
    return shared(t);
}

export function richB(n) {
    const t = n * 2;
    return shared(t);
}
