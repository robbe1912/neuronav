package main

import (
	"fmt"

	"example.com/k334/lib"
)

// Reader is satisfied structurally: Store covers Read+Close without
// declaring the interface anywhere.
type Reader interface {
	Read(p []byte) error
	Close() error
}

//- @main defines func
func main() {
	var r Reader = OpenStore()
	if err := r.Read(nil); err != nil {
		fmt.Println(err)
	}
	_ = r.Close()
	lib.Help()
	helper()
}

//- @helper defines func
func helper() {}
