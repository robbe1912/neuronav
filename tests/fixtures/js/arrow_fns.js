export function plain(a, b) { return a + b; }
const arrowfn = (x) => { return x + 1; };
const fnexpr = function () { return 2; };
class Shape {
  constructor(w) { this.w = w; }
  get size() { return this.w; }
  set size(v) { this.w = v; }
  area() { return this.w * 2; }
}
const handlers = {
  onClick() { return 1; },
};
function collide() { return 1; }
function collide() { return 3; }
export function useShape() {
  const s = new Shape(3);
  return s.area();
}
