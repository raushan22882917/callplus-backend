package session

import "unicode"

// sttSilenceMs is the VAD pause Sarvam waits before closing an utterance. It was
// 900ms; 600ms still tolerates a normal pause but takes 300ms off every turn.
const sttSilenceMs = 600

// scriptLanguages maps a Unicode script to the Sarvam language spoken in it.
var scriptLanguages = []struct {
	table    *unicode.RangeTable
	language string
}{
	{unicode.Bengali, "bn-IN"},
	{unicode.Gujarati, "gu-IN"},
	{unicode.Gurmukhi, "pa-IN"},
	{unicode.Oriya, "od-IN"},
	{unicode.Tamil, "ta-IN"},
	{unicode.Telugu, "te-IN"},
	{unicode.Kannada, "kn-IN"},
	{unicode.Malayalam, "ml-IN"},
}

// ttsLanguage picks the voice language for one chunk of reply text. The agent
// answers in the caller's language, which may differ from the agent's configured
// one, and Sarvam's TTS reads text badly when told the wrong language_code.
// Devanagari is shared by Hindi and Marathi, so the agent's own choice wins
// between them. Latin text is read as Indian English.
func ttsLanguage(text, agentLanguage string) string {
	counts := map[string]int{}
	latin := 0
	for _, r := range text {
		switch {
		case unicode.Is(unicode.Devanagari, r):
			counts["hi-IN"]++
		case unicode.Is(unicode.Latin, r):
			latin++
		default:
			for _, s := range scriptLanguages {
				if unicode.Is(s.table, r) {
					counts[s.language]++
					break
				}
			}
		}
	}
	best, bestN := "", 0
	for lang, n := range counts {
		if n > bestN {
			best, bestN = lang, n
		}
	}
	if bestN == 0 {
		if latin > 0 {
			return "en-IN"
		}
		return fallbackLanguage(agentLanguage)
	}
	if best == "hi-IN" && agentLanguage == "mr-IN" {
		return "mr-IN"
	}
	// A reply that is mostly Latin with a few native words stays in the caller's
	// native language only if the native script dominates.
	if latin > bestN*2 {
		return "en-IN"
	}
	return best
}

func fallbackLanguage(agentLanguage string) string {
	if agentLanguage == "" {
		return "en-IN"
	}
	return agentLanguage
}
