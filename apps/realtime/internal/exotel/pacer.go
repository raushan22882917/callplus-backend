package exotel

import "sync"

// Exotel wants outbound audio in chunks that are a multiple of 320 bytes and at least 3200 bytes
// (100ms); other sizes leave gaps in playback. The engine sends clips of any size, so they are
// collected here and cut to size.
const (
	chunkUnit = 320
	chunkMin  = 3200
)

type chunker struct {
	mu  sync.Mutex
	buf []byte
}

// add appends audio and returns every complete chunk that can be sent now.
func (c *chunker) add(pcm []byte) [][]byte {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.buf = append(c.buf, pcm...)
	var out [][]byte
	for len(c.buf) >= chunkMin {
		n := len(c.buf) / chunkUnit * chunkUnit
		out = append(out, append([]byte(nil), c.buf[:n]...))
		c.buf = c.buf[n:]
	}
	return out
}

// flush returns whatever is left, padded with silence to a valid chunk size, or nil if empty.
func (c *chunker) flush() []byte {
	c.mu.Lock()
	defer c.mu.Unlock()
	if len(c.buf) == 0 {
		return nil
	}
	n := (len(c.buf) + chunkUnit - 1) / chunkUnit * chunkUnit
	if n < chunkMin {
		n = chunkMin
	}
	out := make([]byte, n) // zero bytes are silence in 16-bit PCM
	copy(out, c.buf)
	c.buf = nil
	return out
}

// reset throws away unsent audio (the caller started talking over the agent).
func (c *chunker) reset() {
	c.mu.Lock()
	c.buf = nil
	c.mu.Unlock()
}
