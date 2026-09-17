export function orphan() { return 1; }
export function gone() { return 2; }
function lostA() { return 3; }
function lostB() { return 4; }
function lostC() { return 5; }
function lostD() { return 6; }
class Registry extends ReactComponent {
  _priv() { return 7; }
}
class Counter {
  constructor() { this.n = 0; }
  get value() { return this.n; }
  bump() { this.n += 1; return this.n; }
}
export function useCounter() {
  const c = new Counter();
  c.bump();
  return c.value;
}
