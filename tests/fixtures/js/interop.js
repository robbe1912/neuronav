import Local from './anon_default.js';
import x from './cjs_anon.cjs';
import makeThing from './cjs_named.cjs';
export function run() { return Local() + x() + makeThing(); }
