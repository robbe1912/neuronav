export async function go() {
  const mod = await import('./lazy/mod.js');
  return mod.lazychunk();
}
const p = `./lazy/elsewhere.js`;
import(p);
