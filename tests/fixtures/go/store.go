package main

// Store satisfies main.go's Reader by method-name coverage alone.
type Store struct {
	buf []byte
}

func init() { _ = 0 }

//- @OpenStore defines func
func OpenStore() *Store { return &Store{} }

func (s *Store) Read(p []byte) error { return nil }

func (s *Store) Close() error { return nil }

// Save is exported but nothing calls it: dead via the honest tier
// (exported only saves a fn in an IMPORTED package; same-package
// exported-but-uncalled stays dead-eligible).
//- @Save defines func
func (s *Store) Save() error { return nil }
