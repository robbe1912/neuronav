import { Child } from './child.jsx';
export function render() {
  return <Child onDone={handleDone} />;
}
function handleDone() { return 1; }
