//- @gone dead
export function gone(): number { return 1; }

export function orphan(): number { return 0; }

function lostA(): void { }
function lostB(): void { }
function lostC(): void { }
function lostD(): void { }

class Registry extends ReactComponent {
  _priv(): void { }
}

export class Counter {
  count = 0;
  get value(): number { return this.count; }
  bump(): void { this.count += 1; }
}
