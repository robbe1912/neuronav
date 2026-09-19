package lib

// Help is the package's exported surface: alive while main imports it.
func Help() string { return "help" }

// hidden is package-private and uncalled: dead.
func hidden() {}
