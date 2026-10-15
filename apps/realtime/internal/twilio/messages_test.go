package twilio

import (
	"encoding/json"
	"testing"
)

func TestDecodeStartMessage(t *testing.T) {
	raw := `{
		"event": "start",
		"sequenceNumber": "1",
		"streamSid": "MZxxxx",
		"start": {
			"accountSid": "ACxxxx",
			"streamSid": "MZxxxx",
			"callSid": "CAxxxx",
			"tracks": ["inbound"],
			"mediaFormat": {"encoding": "audio/x-mulaw", "sampleRate": 8000, "channels": 1},
			"customParameters": {"callId": "internal-call-123", "tenantId": "tenant-42"}
		}
	}`

	var msg InboundMessage
	if err := json.Unmarshal([]byte(raw), &msg); err != nil {
		t.Fatalf("unmarshal failed: %v", err)
	}

	if msg.Event != "start" {
		t.Fatalf("expected event 'start', got %q", msg.Event)
	}
	if msg.Start == nil {
		t.Fatal("expected Start payload to be populated")
	}
	if msg.Start.CallSID != "CAxxxx" {
		t.Errorf("expected CallSID CAxxxx, got %q", msg.Start.CallSID)
	}
	if msg.Start.MediaFormat.Encoding != "audio/x-mulaw" || msg.Start.MediaFormat.SampleRate != 8000 {
		t.Errorf("unexpected media format: %+v", msg.Start.MediaFormat)
	}
	if msg.Start.CustomParameters["callId"] != "internal-call-123" {
		t.Errorf("expected callId custom parameter, got %+v", msg.Start.CustomParameters)
	}
}

func TestDecodeMediaMessage(t *testing.T) {
	raw := `{
		"event": "media",
		"sequenceNumber": "3",
		"streamSid": "MZxxxx",
		"media": {"track": "inbound", "chunk": "1", "timestamp": "5", "payload": "base64data"}
	}`

	var msg InboundMessage
	if err := json.Unmarshal([]byte(raw), &msg); err != nil {
		t.Fatalf("unmarshal failed: %v", err)
	}
	if msg.Media == nil || msg.Media.Payload != "base64data" {
		t.Fatalf("expected media payload 'base64data', got %+v", msg.Media)
	}
}

func TestOutboundMediaMessageShape(t *testing.T) {
	msg := NewOutboundMedia("MZxxxx", "base64audio")
	raw, err := json.Marshal(msg)
	if err != nil {
		t.Fatalf("marshal failed: %v", err)
	}

	var decoded map[string]any
	if err := json.Unmarshal(raw, &decoded); err != nil {
		t.Fatalf("unmarshal failed: %v", err)
	}
	if decoded["event"] != "media" {
		t.Errorf("expected event 'media', got %v", decoded["event"])
	}
	if decoded["streamSid"] != "MZxxxx" {
		t.Errorf("expected streamSid 'MZxxxx', got %v", decoded["streamSid"])
	}
	media, ok := decoded["media"].(map[string]any)
	if !ok || media["payload"] != "base64audio" {
		t.Errorf("expected media.payload 'base64audio', got %+v", decoded["media"])
	}
}

func TestOutboundClearMessageHasNoMediaField(t *testing.T) {
	msg := NewOutboundClear("MZxxxx")
	raw, err := json.Marshal(msg)
	if err != nil {
		t.Fatalf("marshal failed: %v", err)
	}
	if string(raw) != `{"event":"clear","streamSid":"MZxxxx"}` {
		t.Errorf("unexpected clear message shape: %s", raw)
	}
}
