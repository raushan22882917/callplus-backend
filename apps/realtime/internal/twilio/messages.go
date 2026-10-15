// Package twilio holds the JSON shapes for Twilio Media Streams, confirmed
// against Twilio's docs (twilio.com/docs/voice/media-streams/websocket-messages):
// audio is always audio/x-mulaw at 8000Hz mono — that's not configurable,
// so this package never deals with any other encoding.
package twilio

// InboundMessage is the envelope for every message Twilio sends. Only
// `Event` is guaranteed present; the matching field below is populated.
type InboundMessage struct {
	Event          string        `json:"event"`
	SequenceNumber string        `json:"sequenceNumber,omitempty"`
	StreamSID      string        `json:"streamSid,omitempty"`
	Start          *StartPayload `json:"start,omitempty"`
	Media          *MediaPayload `json:"media,omitempty"`
	Stop           *StopPayload  `json:"stop,omitempty"`
	Mark           *MarkPayload  `json:"mark,omitempty"`
	DTMF           *DTMFPayload  `json:"dtmf,omitempty"`
}

type StartPayload struct {
	AccountSID       string            `json:"accountSid"`
	StreamSID        string            `json:"streamSid"`
	CallSID          string            `json:"callSid"`
	Tracks           []string          `json:"tracks"`
	MediaFormat      MediaFormat       `json:"mediaFormat"`
	CustomParameters map[string]string `json:"customParameters"`
}

type MediaFormat struct {
	Encoding   string `json:"encoding"`
	SampleRate int    `json:"sampleRate"`
	Channels   int    `json:"channels"`
}

type MediaPayload struct {
	Track     string `json:"track"`
	Chunk     string `json:"chunk"`
	Timestamp string `json:"timestamp"`
	Payload   string `json:"payload"` // base64 mulaw
}

type StopPayload struct {
	AccountSID string `json:"accountSid"`
	CallSID    string `json:"callSid"`
}

type MarkPayload struct {
	Name string `json:"name"`
}

type DTMFPayload struct {
	Track string `json:"track"`
	Digit string `json:"digit"`
}

// Outbound messages — what the gateway sends back to Twilio.

type OutboundMedia struct {
	Event     string            `json:"event"`
	StreamSID string            `json:"streamSid"`
	Media     OutboundMediaBody `json:"media"`
}

type OutboundMediaBody struct {
	Payload string `json:"payload"` // base64 mulaw
}

func NewOutboundMedia(streamSID, base64Payload string) OutboundMedia {
	return OutboundMedia{
		Event:     "media",
		StreamSID: streamSID,
		Media:     OutboundMediaBody{Payload: base64Payload},
	}
}

type OutboundMark struct {
	Event     string           `json:"event"`
	StreamSID string           `json:"streamSid"`
	Mark      OutboundMarkBody `json:"mark"`
}

type OutboundMarkBody struct {
	Name string `json:"name"`
}

func NewOutboundMark(streamSID, name string) OutboundMark {
	return OutboundMark{Event: "mark", StreamSID: streamSID, Mark: OutboundMarkBody{Name: name}}
}

// OutboundClear stops/discards audio Twilio hasn't played yet — the
// barge-in mechanism used when the caller starts speaking over the agent.
type OutboundClear struct {
	Event     string `json:"event"`
	StreamSID string `json:"streamSid"`
}

func NewOutboundClear(streamSID string) OutboundClear {
	return OutboundClear{Event: "clear", StreamSID: streamSID}
}
