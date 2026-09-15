//- @maxf defines func
//- @maxf ret T
export function maxf<T>(a: T, b: T): T { return a; }

class Computer {
  compute<T>(x: T): T { return x; }
}

//- @usegen defines func
export function usegen(): void {
  const one = maxf<number>(1, 2);
  const c = new Computer();
  c.compute<string>("x");
}
