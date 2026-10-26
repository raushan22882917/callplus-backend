package exotel

// G.711 mu-law <-> 16-bit linear PCM. The gateway speaks mu-law 8kHz (Twilio's format) to the
// call engine; Exotel streams 16-bit little-endian PCM, so the bridge converts between them.

const (
	muBias = 0x84
	muClip = 32635
)

// mulawToPCM16 expands mu-law bytes into 16-bit little-endian PCM (two bytes per sample).
func mulawToPCM16(in []byte) []byte {
	out := make([]byte, len(in)*2)
	for i, b := range in {
		s := mulawDecode(b)
		out[2*i] = byte(s)
		out[2*i+1] = byte(s >> 8)
	}
	return out
}

// pcm16ToMulaw compresses 16-bit little-endian PCM into mu-law. A trailing odd byte is dropped.
func pcm16ToMulaw(in []byte) []byte {
	out := make([]byte, len(in)/2)
	for i := range out {
		s := int16(uint16(in[2*i]) | uint16(in[2*i+1])<<8)
		out[i] = mulawEncode(s)
	}
	return out
}

func mulawDecode(b byte) int16 {
	b = ^b
	sign := b & 0x80
	exponent := (b >> 4) & 0x07
	mantissa := b & 0x0F
	sample := ((int(mantissa) << 3) + muBias) << exponent
	sample -= muBias
	if sign != 0 {
		return int16(-sample)
	}
	return int16(sample)
}

func mulawEncode(s int16) byte {
	sample := int(s)
	sign := 0
	if sample < 0 {
		sample = -sample
		sign = 0x80
	}
	if sample > muClip {
		sample = muClip
	}
	sample += muBias
	exponent := 7
	for mask := 0x4000; sample&mask == 0 && exponent > 0; mask >>= 1 {
		exponent--
	}
	mantissa := (sample >> (exponent + 3)) & 0x0F
	return ^byte(sign | exponent<<4 | mantissa)
}
