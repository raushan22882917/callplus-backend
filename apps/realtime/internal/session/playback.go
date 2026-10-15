package session

import (
	"encoding/base64"
	"strings"
	"time"
	"unicode/utf8"
)

// mulawBytesPerSecond: Twilio Media Streams audio is always mulaw, 8kHz,
// mono, one byte per sample.
const mulawBytesPerSecond = 8000

// playbackMargin pads the estimated playback time so network jitter in
// Twilio's own buffering doesn't flip the session back to "not speaking"
// while the caller can still hear the agent.
const playbackMargin = 300 * time.Millisecond

// mulawDuration is how long a base64 mulaw clip takes to play.
func mulawDuration(base64Mulaw string) time.Duration {
	n := base64.StdEncoding.DecodedLen(len(base64Mulaw))
	n -= strings.Count(base64Mulaw[max(0, len(base64Mulaw)-2):], "=")
	if n <= 0 {
		return 0
	}
	return time.Duration(n) * time.Second / mulawBytesPerSecond
}

const (
	// firstChunkMinChars: don't send a tiny first fragment ("Ok.") on its own
	// — it makes speech choppy without saving meaningful latency.
	firstChunkMinChars = 12
	// laterChunkMaxChars bounds the merged remainder so each TTS request is
	// short enough to synthesize quickly but the request count stays small.
	laterChunkMaxChars = 220
)

// splitReply breaks a reply into TTS chunks so the first sentence can start
// playing while the rest is still being synthesized: the first sentence
// alone (for time-to-first-audio), then the remainder merged into chunks of
// at most laterChunkMaxChars.
func splitReply(text string) []string {
	sentences := splitSentences(text)
	if len(sentences) == 0 {
		return nil
	}

	var chunks []string
	rest := sentences
	if utf8.RuneCountInString(sentences[0]) >= firstChunkMinChars {
		chunks = append(chunks, sentences[0])
		rest = sentences[1:]
	}

	current := ""
	for _, sentence := range rest {
		if current != "" && utf8.RuneCountInString(current)+utf8.RuneCountInString(sentence)+1 > laterChunkMaxChars {
			chunks = append(chunks, current)
			current = sentence
			continue
		}
		if current == "" {
			current = sentence
		} else {
			current += " " + sentence
		}
	}
	if current != "" {
		chunks = append(chunks, current)
	}
	return chunks
}

// splitSentences splits after '.', '!', '?', or the Devanagari danda '।'
// when followed by whitespace or end of text — so decimals ("3.5") and
// numbers like "1.800" stay intact.
func splitSentences(text string) []string {
	text = strings.TrimSpace(text)
	if text == "" {
		return nil
	}
	runes := []rune(text)
	var sentences []string
	start := 0
	for i, r := range runes {
		if r != '.' && r != '!' && r != '?' && r != '।' {
			continue
		}
		atEnd := i == len(runes)-1
		if !atEnd && runes[i+1] != ' ' && runes[i+1] != '\n' {
			continue
		}
		if sentence := strings.TrimSpace(string(runes[start : i+1])); sentence != "" {
			sentences = append(sentences, sentence)
		}
		start = i + 1
	}
	if start < len(runes) {
		if tail := strings.TrimSpace(string(runes[start:])); tail != "" {
			sentences = append(sentences, tail)
		}
	}
	return sentences
}
