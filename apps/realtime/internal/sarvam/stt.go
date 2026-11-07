// Package sarvam wraps Sarvam AI's streaming STT and REST TTS APIs.
//
// Protocol confirmed against docs.sarvam.ai's realtime streaming reference
// (wss://api.sarvam.ai/speech-to-text-realtime/ws): encoding=mulaw at
// sample_rate=8000 is an accepted combination (encoding also allows
// linear16/linear32/alaw), matching Twilio's Media Streams audio exactly —
// no transcoding needed.
package sarvam

import (
	"encoding/json"
	"fmt"
	"log"
	"net/http"
	"net/url"

	"github.com/gorilla/websocket"
)

const sttWebSocketURL = "wss://api.sarvam.ai/speech-to-text-realtime/ws"

// STTStream is a live connection to Sarvam's realtime STT endpoint for one
// call. Audio in, transcripts out — the caller drives both directions.
type STTStream struct {
	conn *websocket.Conn
}

type sttAudioInput struct {
	Event string `json:"event"`
	Audio string `json:"audio"`
}

// sttServerEvent covers every event Sarvam's realtime STT can send:
// transcript.partial/final, session.begin/end, vad.speech_start/end,
// config.updated, pong, and error. Fields not relevant to a given event are
// simply left zero-valued.
type sttServerEvent struct {
	Event    string `json:"event"`
	Text     string `json:"text"`
	Message  string `json:"message"`
	Code     string `json:"code"`
	Language string `json:"language"`
}

// DialSTT opens Sarvam's realtime STT stream for one call. silenceDurationMs
// controls the VAD endpointing pause length Sarvam waits before treating an
// utterance as finished (Sarvam's own default is 500ms) — callers can pass 0
// to use that default, or a longer pause so callers aren't cut off mid-
// sentence on a brief hesitation.
func DialSTT(apiKey, languageCode string, silenceDurationMs int) (*STTStream, error) {
	query := url.Values{
		"language_code": {languageCode},
		"model":         {"saaras:v3-realtime"},
		"encoding":      {"mulaw"},
		"sample_rate":   {"8000"},
		"mode":          {"transcribe"},
		"endpointing":   {"vad"},
	}
	if silenceDurationMs > 0 {
		query.Set("silence_duration_ms", fmt.Sprintf("%d", silenceDurationMs))
	}
	dialURL := sttWebSocketURL + "?" + query.Encode()

	header := http.Header{}
	header.Set("API-SUBSCRIPTION-KEY", apiKey)

	conn, resp, err := websocket.DefaultDialer.Dial(dialURL, header)
	if err != nil {
		if resp != nil {
			return nil, fmt.Errorf("dial sarvam stt: %w (http status %s)", err, resp.Status)
		}
		return nil, fmt.Errorf("dial sarvam stt: %w", err)
	}
	return &STTStream{conn: conn}, nil
}

// SendAudio forwards one chunk of base64-encoded mulaw audio (exactly what
// Twilio's media.payload already is — no re-encoding needed).
func (s *STTStream) SendAudio(base64Mulaw string) error {
	return s.conn.WriteJSON(sttAudioInput{Event: "audio_input", Audio: base64Mulaw})
}

// EventKind classifies what Next returned.
type EventKind int

const (
	EventIgnored EventKind = iota
	EventPartial
	EventFinal
	EventSpeechStart
	EventSpeechEnd
)

// Event is one meaningful message from the STT stream. Text is set for
// partial/final transcripts only. Language is the BCP-47 code Saaras detected
// when the socket was opened with language_code=auto (all 23 languages).
type Event struct {
	Kind     EventKind
	Text     string
	Language string
}

// Next blocks for the next server message. Session/pong/config events come
// back as EventIgnored so the caller's loop can just `continue`. VAD
// speech_start/speech_end are surfaced so the session can tell the caller is
// talking before any transcript text exists. "error" is returned as an error:
// Sarvam sends it for real protocol/quota failures, which previously went
// unnoticed and made STT failures look like the caller just wasn't heard.
func (s *STTStream) Next() (Event, error) {
	_, raw, err := s.conn.ReadMessage()
	if err != nil {
		return Event{}, err
	}
	var event sttServerEvent
	if err := json.Unmarshal(raw, &event); err != nil {
		return Event{}, fmt.Errorf("decode stt event: %w", err)
	}
	switch event.Event {
	case "transcript.final":
		return Event{Kind: EventFinal, Text: event.Text, Language: event.Language}, nil
	case "transcript.partial":
		return Event{Kind: EventPartial, Text: event.Text}, nil
	case "vad.speech_start":
		return Event{Kind: EventSpeechStart}, nil
	case "vad.speech_end":
		return Event{Kind: EventSpeechEnd}, nil
	case "error":
		return Event{}, fmt.Errorf("sarvam stt error (code=%s): %s", event.Code, event.Message)
	default:
		log.Printf("[stt] event: %s", event.Event)
		return Event{Kind: EventIgnored}, nil
	}
}

func (s *STTStream) Close() error {
	return s.conn.Close()
}
