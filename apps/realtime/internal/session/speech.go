package session

import (
	"callcenter/realtime/internal/backend"
	"callcenter/realtime/internal/deepgram"
	"callcenter/realtime/internal/sarvam"
)

// sttStream is what the session needs from a live speech recogniser: audio in, events out. Sarvam (India) and
// Deepgram (everywhere else) both provide it.
type sttStream interface {
	SendAudio(base64Mulaw string) error
	Next() (sarvam.Event, error)
	Close() error
}

// dialSTT connects the recogniser this call's agent chose. Deepgram needs the workspace's own key; without one
// the call uses Sarvam, so it never starts without ears.
func (s *Session) dialSTT(cfg *backend.CallConfig, sarvamKey, language string) (sttStream, error) {
	if cfg.STTProvider == "deepgram" && cfg.STTAPIKey != "" {
		spoken := cfg.STTLanguage
		if spoken == "" {
			spoken = language
		}
		return deepgram.Dial(cfg.STTAPIKey, spoken, sttSilenceMs)
	}
	return sarvam.DialSTT(sarvamKey, sttLanguage(language), sttSilenceMs)
}
