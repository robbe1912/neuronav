package main

import "testing"

func TestUtil(t *testing.T) {
	if !checkGold() {
		t.Fatal("gold")
	}
}

func checkGold() bool { return true }
