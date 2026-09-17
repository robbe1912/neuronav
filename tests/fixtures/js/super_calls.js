import { Base } from './base_cls.js';
export class Sub extends Base {
  tick() { super.tick(); return 2; }
  drive() { this.tick(); }
}
