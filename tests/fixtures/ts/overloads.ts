//- @ship defines func
function ship(a: string): string;
function ship(a: number): number;
function ship(a: any): any { return a; }

//- @caller defines func
//- @caller calls @ship
export function caller(): number { return ship(1); }
