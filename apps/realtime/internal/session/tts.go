package session

import (
	"log"
	"time"

	"callcenter/realtime/internal/sarvam"
)

// wsRetryAfter: after the TTS WebSocket fails before delivering any audio, use
// the REST endpoint for this long before trying the socket again, so a blocked
// or rate-limited socket costs one slow sentence, not one per sentence.
const wsRetryAfter = 30 * time.Second

// socketFor returns the call's persistent TTS connection for a language, or nil
// when WebSockets are off or recently failed (callers then use REST).
func (s *Session) socketFor(language string) *sarvam.TTSSocket {
	if !s.cfg.TTSWebSocket || s.sarvamKey == "" {
		return nil
	}
	s.ttsMu.Lock()
	defer s.ttsMu.Unlock()
	if time.Now().Before(s.ttsDisabledUntil) {
		return nil
	}
	if s.ttsSockets == nil {
		s.ttsSockets = map[string]*sarvam.TTSSocket{}
	}
	sock, ok := s.ttsSockets[language]
	if !ok {
		sock = sarvam.NewTTSSocket(s.sarvamKey, language, s.voice)
		s.ttsSockets[language] = sock
	}
	return sock
}

// warmTTS opens the connections a reply is most likely to need.
func (s *Session) warmTTS() {
	for _, language := range uniqueStrings(fallbackLanguage(s.language), "en-IN") {
		sock := s.socketFor(language)
		if sock == nil {
			return
		}
		if err := sock.Warm(); err != nil {
			log.Printf("[session %s] tts websocket warm-up failed, will use REST: %v", s.callID, err)
			s.disableTTSSockets()
			return
		}
	}
}

func (s *Session) disableTTSSockets() {
	s.ttsMu.Lock()
	s.ttsDisabledUntil = time.Now().Add(wsRetryAfter)
	s.ttsMu.Unlock()
}

func (s *Session) closeTTSSockets() {
	s.ttsMu.Lock()
	sockets := s.ttsSockets
	s.ttsSockets = nil
	s.ttsDisabledUntil = time.Now().Add(24 * time.Hour)
	s.ttsMu.Unlock()
	for _, sock := range sockets {
		sock.Close()
	}
}

// synthesize turns one chunk of reply text into audio. Over the persistent
// WebSocket the pieces arrive while the sentence is still being generated, so
// playback starts on the first; if the socket can't deliver anything, REST does.
func (s *Session) synthesize(chunk string) <-chan ttsResult {
	language := ttsLanguage(chunk, s.language)
	out := make(chan ttsResult, 64)
	go func() {
		defer close(out)
		if sock := s.socketFor(language); sock != nil {
			delivered, err := sock.Stream(chunk, func(audio string) { out <- ttsResult{audio: audio} })
			if err == nil {
				return
			}
			if delivered > 0 {
				out <- ttsResult{err: err} // part of the sentence was already spoken
				return
			}
			log.Printf("[session %s] tts websocket failed, using REST for this sentence: %v", s.callID, err)
			s.disableTTSSockets()
		}
		audio, err := s.tts.Synthesize(chunk, language, s.voice)
		out <- ttsResult{audio: audio, err: err}
	}()
	return out
}

func uniqueStrings(values ...string) []string {
	seen := map[string]bool{}
	var out []string
	for _, v := range values {
		if !seen[v] {
			seen[v] = true
			out = append(out, v)
		}
	}
	return out
}
