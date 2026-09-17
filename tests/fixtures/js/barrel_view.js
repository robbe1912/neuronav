import { alpha } from './barrel_esm.js';
const { beta } = require('./barrel_cjs.cjs');
export function useBoth() { return alpha() + beta(); }
