//- const Red = <Color>
//- const Green = <Color>
//- const Blue = <Color>
//- alias Pair = type
//- alias Shape = interface
//- ! @area defines func
enum Color { Red, Green = 5, Blue }

type Pair = { a: number; b: string };

interface Shape { area(): number; }

export function tone(c: Color): number { return 6; }
