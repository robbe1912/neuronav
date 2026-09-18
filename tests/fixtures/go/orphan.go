package main

import "fmt"

// Orphan carries the fmt.Stringer shield: String has no textual call
// site anywhere but the fmt machinery dispatches it.
type Orphan struct{}

//- @String defines func
func (o Orphan) String() string { return "orphan" }

// forgotten has no callers and no shield: honest likely-dead.
//- @forgotten dead
func forgotten() { fmt.Println("never") }
