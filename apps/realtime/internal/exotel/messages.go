// Package exotel bridges Exotel's Voicebot WebSocket to the call engine. Exotel streams 16-bit
// little-endian PCM at 8kHz as JSON events (connected, start, media, mark, dtmf, stop), documented
// at developer.exotel.com/docs/agentstream. The call engine speaks Twilio's mu-law Media Streams
// shape, so the bridge translates between the two and the engine itself stays unchanged.
package exotel

import "encoding/json"

// inbound is every event Exotel sends us.
type inbound struct {
	Event          string          `json:"event"`
	SequenceNumber json.RawMessage `json:"sequence_number,omitempty"`
	StreamSID      string          `json:"stream_sid,omitempty"`
	Start          *startBody      `json:"start,omitempty"`
	Media          *mediaBody      `json:"media,omitempty"`
	Mark           *markBody       `json:"mark,omitempty"`
	Stop           *stopBody       `json:"stop,omitempty"`
}

type startBody struct {
	StreamSID        string            `json:"stream_sid"`
	CallSID          string            `json:"call_sid"`
	AccountSID       string            `json:"account_sid"`
	From             string            `json:"from"`
	To               string            `json:"to"`
	CustomParameters map[string]string `json:"custom_parameters"`
}

type mediaBody struct {
	Chunk     json.RawMessage `json:"chunk,omitempty"`
	Timestamp json.RawMessage `json:"timestamp,omitempty"`
	Payload   string          `json:"payload"` // base64 16-bit PCM
}

type markBody struct {
	Name string `json:"name"`
}

type stopBody struct {
	CallSID string `json:"call_sid"`
	Reason  string `json:"reason"`
}

// Messages we send to Exotel.

type outMedia struct {
	Event     string       `json:"event"`
	StreamSID string       `json:"stream_sid"`
	Media     outMediaBody `json:"media"`
}

type outMediaBody struct {
	Payload string `json:"payload"`
}

type outMark struct {
	Event     string   `json:"event"`
	StreamSID string   `json:"stream_sid"`
	Mark      markBody `json:"mark"`
}

type outClear struct {
	Event     string `json:"event"`
	StreamSID string `json:"stream_sid"`
}
