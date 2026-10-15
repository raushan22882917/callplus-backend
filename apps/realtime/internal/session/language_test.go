package session

import "testing"

func TestTTSLanguage(t *testing.T) {
	cases := []struct{ text, agent, want string }{
		{"Namaste, how can I help?", "hi-IN", "en-IN"},
		{"नमस्ते, मैं आपकी कैसे मदद कर सकता हूँ?", "en-IN", "hi-IN"},
		{"तुमचं स्वागत आहे", "mr-IN", "mr-IN"},
		{"வணக்கம், எப்படி உதவலாம்?", "en-IN", "ta-IN"},
		{"నమస్కారం", "hi-IN", "te-IN"},
		{"ਸਤ ਸ੍ਰੀ ਅਕਾਲ", "en-IN", "pa-IN"},
		{"12345", "ta-IN", "ta-IN"},
		{"", "", "en-IN"},
	}
	for _, c := range cases {
		if got := ttsLanguage(c.text, c.agent); got != c.want {
			t.Errorf("ttsLanguage(%q,%q)=%q want %q", c.text, c.agent, got, c.want)
		}
	}
}
