//- @arrowfn defines func
//- @arrowfn ret number
export const arrowfn = (a: number): number => a * 2;

const fnexpr = function (b: string): string { return b + "!"; };

class Widget {
  name: string;
  constructor(n: string) { this.name = n; }
  shape(): number { return 1; }
  get size(): number { return 2; }
  set size(v: number) { }
}
//- @collide defines func
export function collide(): number { return 3; }
export function collide(v: number): number { return 4; }

//- @user defines func
//- @user calls @arrowfn
export function user(): number { return arrowfn(3); }
