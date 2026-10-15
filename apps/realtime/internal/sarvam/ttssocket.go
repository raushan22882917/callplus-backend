package sarvam

import (
	"errors"
	"fmt"
	"net/http"
	"sync"
	"time"

	"github.com/gorilla/websocket"
)

const ttsSocketURL = "wss://api.sarvam.ai/text-to-speech/ws?model=bulbul:v3&send_completion_event=true"

// ttsKeepAlive: Sarvam closes an idle TTS socket after about a minute.
const ttsKeepAlive = 25 * time.Second

// ttsReadTimeout bounds the wait for the next audio message of one sentence.
const ttsReadTimeout = 15 * time.Second

// TTSSocket is one persistent Sarvam text-to-speech WebSocket for a single
// language and voice. Opening it once at the start of a call removes the
// connection and per-request cost the REST endpoint pays for every sentence:
// measured live, the first audio chunk of a sentence arrives in about 0.23s,
// against 0.9-1.3s for the REST call. Audio is raw mulaw/8kHz, so each base64
// chunk goes straight to Twilio.
//
// Sentences are synthesized one at a time (the socket's protocol is a single
// text -> audio -> "final" exchange per flush); callers queue them.
type TTSSocket struct {
	url      string
	apiKey   string
	language string
	speaker  string

	mu       sync.Mutex // one sentence at a time; also guards conn
	conn     *websocket.Conn
	lastUsed time.Time
	closed   bool
	stopKeep chan struct{}
}

func NewTTSSocket(apiKey, language, speaker string) *TTSSocket {
	return newTTSSocket(ttsSocketURL, apiKey, language, speaker)
}

func newTTSSocket(url, apiKey, language, speaker string) *TTSSocket {
	return &TTSSocket{url: url, apiKey: apiKey, language: language, speaker: speaker, stopKeep: make(chan struct{})}
}

type ttsConfigMessage struct {
	Type string `json:"type"`
	Data struct {
		Speaker          string `json:"speaker,omitempty"`
		TargetLanguage   string `json:"target_language_code"`
		LanguageCode     string `json:"language_code"`
		OutputAudioCodec string `json:"output_audio_codec"`
		SampleRate       int    `json:"speech_sample_rate"`
		MinBufferSize    int    `json:"min_buffer_size"`
		MaxChunkLength   int    `json:"max_chunk_length"`
	} `json:"data"`
}

type ttsServerMessage struct {
	Type string `json:"type"`
	Data struct {
		Audio     string `json:"audio"`
		EventType string `json:"event_type"`
		Message   string `json:"message"`
	} `json:"data"`
}

// dial opens the socket and sends the mandatory first "config" message. The
// caller holds t.mu.
func (t *TTSSocket) dial() error {
	header := http.Header{}
	header.Set("API-SUBSCRIPTION-KEY", t.apiKey)
	dialer := *websocket.DefaultDialer
	dialer.HandshakeTimeout = 5 * time.Second
	conn, resp, err := dialer.Dial(t.url, header)
	if err != nil {
		if resp != nil {
			return fmt.Errorf("dial sarvam tts: %w (http status %s)", err, resp.Status)
		}
		return fmt.Errorf("dial sarvam tts: %w", err)
	}
	var config ttsConfigMessage
	config.Type = "config"
	config.Data.Speaker = t.speaker
	config.Data.TargetLanguage = t.language
	config.Data.LanguageCode = t.language
	config.Data.OutputAudioCodec = "mulaw"
	config.Data.SampleRate = 8000
	config.Data.MinBufferSize = 30
	config.Data.MaxChunkLength = 150
	if err := conn.WriteJSON(config); err != nil {
		_ = conn.Close()
		return fmt.Errorf("send tts config: %w", err)
	}
	t.conn = conn
	t.lastUsed = time.Now()
	return nil
}

// Warm opens the connection ahead of the first sentence and keeps it alive.
func (t *TTSSocket) Warm() error {
	t.mu.Lock()
	defer t.mu.Unlock()
	if t.closed {
		return errors.New("tts socket closed")
	}
	if t.conn == nil {
		if err := t.dial(); err != nil {
			return err
		}
	}
	t.startKeepAliveLocked()
	return nil
}

func (t *TTSSocket) startKeepAliveLocked() {
	if t.stopKeep == nil {
		return
	}
	stop := t.stopKeep
	t.stopKeep = nil // start only one keep-alive loop
	go func() {
		ticker := time.NewTicker(ttsKeepAlive)
		defer ticker.Stop()
		for {
			select {
			case <-stop:
				return
			case <-ticker.C:
				if !t.mu.TryLock() { // a sentence is in flight, so the socket is busy
					continue
				}
				if t.conn != nil && time.Since(t.lastUsed) >= ttsKeepAlive {
					if err := t.conn.WriteJSON(map[string]string{"type": "ping"}); err != nil {
						t.dropLocked()
					}
				}
				t.mu.Unlock()
			}
		}
	}()
}

func (t *TTSSocket) dropLocked() {
	if t.conn != nil {
		_ = t.conn.Close()
		t.conn = nil
	}
}

// Close ends the connection for good.
func (t *TTSSocket) Close() {
	t.mu.Lock()
	defer t.mu.Unlock()
	t.closed = true
	t.dropLocked()
	if t.stopKeep != nil {
		close(t.stopKeep)
		t.stopKeep = nil
	}
}

// Stream synthesizes one sentence, calling onChunk with each base64 mulaw chunk
// the moment it arrives. It returns the number of chunks delivered; if err is
// non-nil and the count is 0 nothing reached the caller, so using the REST
// client for this sentence is safe. A dead connection is re-dialled once
// before any audio has been delivered.
func (t *TTSSocket) Stream(text string, onChunk func(base64Mulaw string)) (delivered int, err error) {
	t.mu.Lock()
	defer t.mu.Unlock()
	if t.closed {
		return 0, errors.New("tts socket closed")
	}
	for attempt := 0; attempt < 2; attempt++ {
		if t.conn == nil {
			if err = t.dial(); err != nil {
				return delivered, err
			}
		}
		n, retry, streamErr := t.streamLocked(text, onChunk)
		delivered += n
		if streamErr == nil {
			t.lastUsed = time.Now()
			return delivered, nil
		}
		t.dropLocked() // never reuse a socket that errored mid-sentence
		err = streamErr
		if !retry || delivered > 0 {
			return delivered, err
		}
	}
	return delivered, err
}

// streamLocked runs one text -> flush -> audio... -> final exchange. retry is
// true when the failure happened before any audio and looks like a stale socket.
func (t *TTSSocket) streamLocked(text string, onChunk func(string)) (n int, retry bool, err error) {
	if err := t.conn.WriteJSON(map[string]any{"type": "text", "data": map[string]string{"text": text}}); err != nil {
		return 0, true, fmt.Errorf("send tts text: %w", err)
	}
	if err := t.conn.WriteJSON(map[string]string{"type": "flush"}); err != nil {
		return 0, true, fmt.Errorf("send tts flush: %w", err)
	}
	for {
		_ = t.conn.SetReadDeadline(time.Now().Add(ttsReadTimeout))
		var msg ttsServerMessage
		if err := t.conn.ReadJSON(&msg); err != nil {
			return n, n == 0, fmt.Errorf("read tts audio: %w", err)
		}
		switch msg.Type {
		case "audio":
			if msg.Data.Audio != "" {
				n++
				onChunk(msg.Data.Audio)
			}
		case "event":
			if msg.Data.EventType == "final" {
				_ = t.conn.SetReadDeadline(time.Time{})
				if n == 0 {
					return 0, false, errors.New("sarvam tts returned no audio")
				}
				return n, false, nil
			}
		case "error":
			return n, false, fmt.Errorf("sarvam tts error: %s", msg.Data.Message)
		}
		// pong and any other housekeeping message: keep reading
	}
}
