function Injectable(): ((t: object) => object) { return (t) => t; }

@Injectable()
class Service {
  m(): void { assist(); }
  n(): void { }
}

function assist(): void { }

class Api {
  @DecoratedGet()
  handler(): void { }
}

function DecoratedGet(): ((t: object, k: string, d: object) => object) {
  return (t, k, d) => d;
}
