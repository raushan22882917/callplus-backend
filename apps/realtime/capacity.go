package main

import "sync/atomic"

// capacity counts the calls this gateway is handling and turns new ones away at a limit, so the
// calls already in progress keep clear audio instead of every call getting worse together.
type capacity struct {
	max    int64
	active atomic.Int64
	peak   atomic.Int64
	denied atomic.Int64
}

func newCapacity(max int) *capacity { return &capacity{max: int64(max)} }

// acquire reserves a place for one call. A limit of zero or less means no limit.
func (c *capacity) acquire() bool {
	n := c.active.Add(1)
	if c.max > 0 && n > c.max {
		c.active.Add(-1)
		c.denied.Add(1)
		return false
	}
	for {
		p := c.peak.Load()
		if n <= p || c.peak.CompareAndSwap(p, n) {
			return true
		}
	}
}

func (c *capacity) release() { c.active.Add(-1) }

// full reports whether a new call would be turned away right now.
func (c *capacity) full() bool { return c.max > 0 && c.active.Load() >= c.max }
