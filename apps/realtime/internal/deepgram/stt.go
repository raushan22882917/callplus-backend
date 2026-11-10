// Package deepgram is the realtime speech-to-text provider for calls outside India: a workspace that connects
// its own Deepgram key lets an agent hear callers in dozens of languages. It speaks the same small interface as
// the Sarvam client (SendAudio / Next / Close) and returns the same event type, so the session does not care
// which one it is talking to.
//
// Deepgram's live endpoint takes 8 kHz mu-law straight from a phone line, so no transcoding is needed.
package deepgram

import (
	"encoding/base64"
	"encoding/json"
	"fmt"
	"net/http"
	"net/url"
	"strings"
	"sync"
	"time"

	"github.com/gorilla/websocket"

	"callcenter/realtime/internal/sarvam"
)

// baseURL is a variable so tests can point it at a local server.
var baseURL = "wss://api.deepgram.com/v1/listen"

const keepAliveEvery = 5 * time.Second

// Stream is one call's live connection to Deepgram.
type Stream struct {
	conn *websocket.Conn
	wmu  sync.Mutex // gorilla allows one writer at a time; audio and keep-alives both write
	done chan struct{}

	// A spoken sentence arrives as several finalised pieces; they are joined and handed on as one transcript
	// when Deepgram says the speaker stopped.
	parts []string
}

type result struct {
	Type        string `json:"type"`
	IsFinal     bool   `json:"is_final"`
	SpeechFinal bool   `json:"speech_final"`
	Channel     struct {
		Alternatives []struct {
			Transcript string   `json:"transcript"`
			Languages  []string `json:"languages"`
		} `json:"alternatives"`
	} `json:"channel"`
	Message string `json:"description"`
	Code    string `json:"err_code"`
}

// Language turns an agent's language setting into the code Deepgram expects. An empty setting means "any of the
// languages Deepgram's multilingual model knows".
func Language(code string) string {
	switch strings.TrimSpace(code) {
	case "":
		return "multi"
	case "es-MX":
		return "es-419"
	default:
		return code
	}
}

// Dial opens the live stream. silenceMs is how long a pause counts as the end of a sentence.
func Dial(apiKey, language string, silenceMs int) (*Stream, error) {
	if silenceMs < 100 {
		silenceMs = 900
	}
	q := url.Values{
		"model":            {"nova-3"},
		"language":         {Language(language)},
		"encoding":         {"mulaw"},
		"sample_rate":      {"8000"},
		"channels":         {"1"},
		"interim_results":  {"true"},
		"punctuate":        {"true"},
		"smart_format":     {"true"},
		"vad_events":       {"true"},
		"endpointing":      {fmt.Sprintf("%d", silenceMs)},
		"utterance_end_ms": {"1000"},
	}
	header := http.Header{}
	header.Set("Authorization", "Token "+apiKey)
	conn, resp, err := websocket.DefaultDialer.Dial(baseURL+"?"+q.Encode(), header)
	if err != nil {
		if resp != nil {
			return nil, fmt.Errorf("dial deepgram: %w (http status %s)", err, resp.Status)
		}
		return nil, fmt.Errorf("dial deepgram: %w", err)
	}
	s := &Stream{conn: conn, done: make(chan struct{})}
	go s.keepAlive()
	return s, nil
}

// keepAlive stops Deepgram closing a stream that has had no audio for a few seconds.
func (s *Stream) keepAlive() {
	t := time.NewTicker(keepAliveEvery)
	defer t.Stop()
	for {
		select {
		case <-s.done:
			return
		case <-t.C:
			s.write(websocket.TextMessage, []byte(`{"type":"KeepAlive"}`))
		}
	}
}

func (s *Stream) write(kind int, data []byte) error {
	s.wmu.Lock()
	defer s.wmu.Unlock()
	return s.conn.WriteMessage(kind, data)
}

// SendAudio forwards one chunk of base64 mu-law audio, exactly as Twilio sends it.
func (s *Stream) SendAudio(base64Mulaw string) error {
	raw, err := base64.StdEncoding.DecodeString(base64Mulaw)
	if err != nil {
		return fmt.Errorf("decode audio: %w", err)
	}
	return s.write(websocket.BinaryMessage, raw)
}

// Next blocks for the next meaningful event. Messages that matter to nobody come back as EventIgnored.
func (s *Stream) Next() (sarvam.Event, error) {
	_, raw, err := s.conn.ReadMessage()
	if err != nil {
		return sarvam.Event{}, err
	}
	var r result
	if err := json.Unmarshal(raw, &r); err != nil {
		return sarvam.Event{}, fmt.Errorf("decode deepgram message: %w", err)
	}
	switch r.Type {
	case "Results":
		return s.results(r), nil
	case "SpeechStarted":
		return sarvam.Event{Kind: sarvam.EventSpeechStart}, nil
	case "UtteranceEnd":
		// The speaker stopped, but no piece carried speech_final: hand over whatever was heard.
		if text := s.flush(); text != "" {
			return sarvam.Event{Kind: sarvam.EventFinal, Text: text}, nil
		}
		return sarvam.Event{Kind: sarvam.EventSpeechEnd}, nil
	case "Error":
		return sarvam.Event{}, fmt.Errorf("deepgram error (code=%s): %s", r.Code, r.Message)
	default:
		return sarvam.Event{Kind: sarvam.EventIgnored}, nil
	}
}

func (s *Stream) results(r result) sarvam.Event {
	if len(r.Channel.Alternatives) == 0 {
		return sarvam.Event{Kind: sarvam.EventIgnored}
	}
	alt := r.Channel.Alternatives[0]
	text := strings.TrimSpace(alt.Transcript)
	if !r.IsFinal {
		if text == "" {
			return sarvam.Event{Kind: sarvam.EventIgnored}
		}
		return sarvam.Event{Kind: sarvam.EventPartial, Text: strings.TrimSpace(strings.Join(append(append([]string{}, s.parts...), text), " "))}
	}
	if text != "" {
		s.parts = append(s.parts, text)
	}
	if !r.SpeechFinal {
		return sarvam.Event{Kind: sarvam.EventIgnored}
	}
	whole := s.flush()
	if whole == "" {
		return sarvam.Event{Kind: sarvam.EventSpeechEnd}
	}
	lang := ""
	if len(alt.Languages) > 0 {
		lang = alt.Languages[0]
	}
	return sarvam.Event{Kind: sarvam.EventFinal, Text: whole, Language: lang}
}

func (s *Stream) flush() string {
	whole := strings.TrimSpace(strings.Join(s.parts, " "))
	s.parts = nil
	return whole
}

// Close ends the stream politely, then drops the connection.
func (s *Stream) Close() error {
	select {
	case <-s.done:
	default:
		close(s.done)
	}
	_ = s.write(websocket.TextMessage, []byte(`{"type":"CloseStream"}`))
	return s.conn.Close()
}
