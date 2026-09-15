//- @drive defines func
import { Base } from "./base_cls";

export class Sub extends Base {
  tick(): void { super.tick(); }
}

export function drive(): void {
  const s = new Sub();
  s.tick();
}
