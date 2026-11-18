package main

import "testing"

func TestCapacityTurnsAwayAtTheLimit(t *testing.T) {
	c := newCapacity(2)
	if !c.acquire() || !c.acquire() {
		t.Fatal("first two calls should fit")
	}
	if c.acquire() {
		t.Fatal("third call should be turned away")
	}
	if c.active.Load() != 2 || c.denied.Load() != 1 || !c.full() {
		t.Fatalf("active=%d denied=%d", c.active.Load(), c.denied.Load())
	}
	c.release()
	if !c.acquire() {
		t.Fatal("a place frees up after a call ends")
	}
	if c.peak.Load() != 2 {
		t.Fatalf("peak=%d", c.peak.Load())
	}
}

func TestCapacityZeroMeansUnlimited(t *testing.T) {
	c := newCapacity(0)
	for i := 0; i < 1000; i++ {
		if !c.acquire() {
			t.Fatal("no limit set")
		}
	}
}
