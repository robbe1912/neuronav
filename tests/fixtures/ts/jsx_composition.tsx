//- @handleDone defines func
//- @render defines func
import Child from "./child";

function handleDone(): void { }

export function render(): JSX.Element {
  return <Child onDone={handleDone} />;
}
