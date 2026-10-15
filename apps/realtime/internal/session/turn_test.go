package session

import "testing"

func TestLooksLikeEcho(t *testing.T) {
	spoken := "Your appointment is confirmed for Tuesday at three o'clock. Is there anything else I can help with?"
	for _, tc := range []struct {
		name  string
		heard string
		want  bool
	}{
		{"agent words coming back", "your appointment is confirmed for Tuesday at three", true},
		{"caller replies normally", "no that is all thank you very much", false},
		{"short yes is never echo", "yes", false},
		{"short repeat of agent word is never echo", "help with", false},
		{"unrelated sentence", "can you move it to Friday morning instead", false},
	} {
		if got := looksLikeEcho(tc.heard, spoken); got != tc.want {
			t.Errorf("%s: looksLikeEcho(%q) = %v, want %v", tc.name, tc.heard, got, tc.want)
		}
	}
}

func TestSTTLanguage(t *testing.T) {
	for in, want := range map[string]string{"en-IN": "auto", "hi-IN": "auto", "": "auto", "ta-IN": "auto"} {
		if got := sttLanguage(in); got != want {
			t.Errorf("sttLanguage(%q) = %q, want %q", in, got, want)
		}
	}
}

func inList(phrase string, list []string) bool {
	for _, p := range list {
		if p == phrase {
			return true
		}
	}
	return false
}

func TestFillerPhraseFollowsCallerLanguage(t *testing.T) {
	if got := fillerPhrase(ttsLanguage("haan booking kar do", "hi-IN")); !inList(got, fillerPhrases["en-IN"]) {
		t.Fatalf("Latin-script caller should get an English filler, got %q", got)
	}
	if got := fillerPhrase(ttsLanguage("कल दो बजे बुक कर दो", "en-IN")); !inList(got, fillerPhrases["hi-IN"]) {
		t.Fatalf("Devanagari caller should get a Hindi filler, got %q", got)
	}
	if got := fillerPhrase("xx-XX"); !inList(got, fillerPhrases["en-IN"]) {
		t.Fatalf("unknown language falls back to English, got %q", got)
	}
}

func TestFillerPhrasesVaryAndExistForEveryLanguage(t *testing.T) {
	for language, phrases := range fillerPhrases {
		if len(phrases) < 2 {
			t.Errorf("%s needs at least two filler phrases so the agent doesn't repeat itself", language)
		}
	}
	seen := map[string]bool{}
	for i := 0; i < 8; i++ {
		seen[fillerPhrase("hi-IN")] = true
	}
	if len(seen) < 2 {
		t.Fatalf("consecutive fillers must differ, saw %v", seen)
	}
	if got := fillerPhrase("xx-YY"); got == "" {
		t.Fatal("an unknown language falls back to English")
	}
}
