package session

import (
	"encoding/base64"
	"reflect"
	"strings"
	"testing"
	"time"
)

func TestMulawDuration(t *testing.T) {
	for _, tc := range []struct {
		bytes int
		want  time.Duration
	}{
		{8000, time.Second},
		{4000, 500 * time.Millisecond},
		{8001, time.Second + time.Second/8000}, // padded base64 length
		{0, 0},
	} {
		encoded := base64.StdEncoding.EncodeToString(make([]byte, tc.bytes))
		if got := mulawDuration(encoded); got != tc.want {
			t.Errorf("%d bytes: got %v, want %v", tc.bytes, got, tc.want)
		}
	}
}

func TestSplitSentences(t *testing.T) {
	got := splitSentences("Your balance is 3.5 dollars. Anything else? Great! नमस्ते। धन्यवाद")
	want := []string{"Your balance is 3.5 dollars.", "Anything else?", "Great!", "नमस्ते।", "धन्यवाद"}
	if !reflect.DeepEqual(got, want) {
		t.Errorf("got %q, want %q", got, want)
	}
	if splitSentences("   ") != nil {
		t.Error("blank text should yield no sentences")
	}
}

func TestSplitReplyFirstSentenceAloneThenMergedRemainder(t *testing.T) {
	got := splitReply("Sure, I can help with that. One moment. Let me check. It is ready.")
	want := []string{"Sure, I can help with that.", "One moment. Let me check. It is ready."}
	if !reflect.DeepEqual(got, want) {
		t.Errorf("got %q, want %q", got, want)
	}
}

func TestSplitReplyShortFirstSentenceIsMerged(t *testing.T) {
	got := splitReply("Ok. I will look that up for you right now.")
	if len(got) != 1 {
		t.Errorf("short first sentence should merge with the rest, got %q", got)
	}
}

func TestSplitReplyBoundsChunkSize(t *testing.T) {
	long := strings.Repeat("This is a sentence of reasonable length. ", 30)
	for _, chunk := range splitReply(long) {
		if len([]rune(chunk)) > laterChunkMaxChars+60 {
			t.Errorf("chunk too long (%d runes)", len([]rune(chunk)))
		}
	}
}
