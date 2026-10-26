package exotel

import (
	"encoding/base64"
	"encoding/json"
	"log"
	"sync"
	"time"

	"github.com/gorilla/websocket"

	"callcenter/realtime/internal/twilio"
)

// idleTimeout: Exotel sends audio continuously while a call is up, so silence on the socket
// means the call is gone.
const idleTimeout = 60 * time.Second

// Config wires the bridge to the rest of the gateway.
type Config struct {
	// EngineURL is the gateway's own Twilio-shaped endpoint, e.g. ws://127.0.0.1:9000/ws/twilio.
	EngineURL string
	Start     StartFunc
}

// Handle bridges one Exotel call until either side hangs up. streamKey is the secret from the
// WebSocket URL that identifies which workspace's Exotel account is calling.
func Handle(ex *websocket.Conn, streamKey string, cfg Config) {
	b := &bridge{ex: ex, key: streamKey, cfg: cfg, done: make(chan struct{})}
	b.run()
}

type bridge struct {
	ex    *websocket.Conn
	exMu  sync.Mutex
	eng   *websocket.Conn
	engMu sync.Mutex
	key   string
	cfg   Config

	streamSID string
	callID    string
	out       chunker

	closeOnce sync.Once
	done      chan struct{}
}

func (b *bridge) run() {
	defer b.close()
	for {
		_ = b.ex.SetReadDeadline(time.Now().Add(idleTimeout))
		_, raw, err := b.ex.ReadMessage()
		if err != nil {
			log.Printf("[exotel %s] read: %v", b.callID, err)
			return
		}
		var m inbound
		if err := json.Unmarshal(raw, &m); err != nil {
			log.Printf("[exotel %s] bad message: %v", b.callID, err)
			continue
		}
		switch m.Event {
		case "start":
			if !b.handleStart(m) {
				return
			}
		case "media":
			b.handleMedia(m.Media)
		case "mark":
			if m.Mark != nil && b.eng != nil {
				b.toEngine(map[string]any{"event": "mark", "streamSid": b.streamSID, "mark": map[string]string{"name": m.Mark.Name}})
			}
		case "stop":
			if b.eng != nil {
				b.toEngine(map[string]any{"event": "stop", "streamSid": b.streamSID, "stop": map[string]string{"callSid": ""}})
			}
			return
		}
	}
}

func (b *bridge) handleStart(m inbound) bool {
	if m.Start == nil {
		return false
	}
	b.streamSID = m.Start.StreamSID
	if b.streamSID == "" {
		b.streamSID = m.StreamSID
	}
	info, err := b.cfg.Start(b.key, m.Start.CallSID, m.Start.From, m.Start.To)
	if err != nil {
		log.Printf("[exotel] call refused: %v", err)
		return false
	}
	b.callID = info.CallID

	eng, _, err := websocket.DefaultDialer.Dial(b.cfg.EngineURL, nil)
	if err != nil {
		log.Printf("[exotel %s] cannot reach the call engine: %v", b.callID, err)
		return false
	}
	b.eng = eng
	b.toEngine(map[string]any{
		"event":     "start",
		"streamSid": b.streamSID,
		"start": twilio.StartPayload{
			AccountSID:       m.Start.AccountSID,
			StreamSID:        b.streamSID,
			CallSID:          m.Start.CallSID,
			Tracks:           []string{"inbound"},
			MediaFormat:      twilio.MediaFormat{Encoding: "audio/x-mulaw", SampleRate: 8000, Channels: 1},
			CustomParameters: map[string]string{"callId": info.CallID, "tenantId": info.TenantID, "closeOnFinalize": "1"},
		},
	})
	go b.pumpEngine()
	return true
}

// handleMedia turns the caller's 16-bit audio into mu-law for the engine.
func (b *bridge) handleMedia(m *mediaBody) {
	if m == nil || b.eng == nil {
		return
	}
	pcm, err := base64.StdEncoding.DecodeString(m.Payload)
	if err != nil || len(pcm) < 2 {
		return
	}
	b.toEngine(twilio.InboundMessage{
		Event:     "media",
		StreamSID: b.streamSID,
		Media:     &twilio.MediaPayload{Track: "inbound", Payload: base64.StdEncoding.EncodeToString(pcm16ToMulaw(pcm))},
	})
}

// pumpEngine carries the agent's speech back to the caller until the engine hangs up.
func (b *bridge) pumpEngine() {
	defer b.close()
	for {
		_, raw, err := b.eng.ReadMessage()
		if err != nil {
			return
		}
		var m twilio.InboundMessage
		if err := json.Unmarshal(raw, &m); err != nil {
			continue
		}
		switch m.Event {
		case "media":
			if m.Media == nil {
				continue
			}
			mulaw, err := base64.StdEncoding.DecodeString(m.Media.Payload)
			if err != nil {
				continue
			}
			for _, chunk := range b.out.add(mulawToPCM16(mulaw)) {
				b.sendMedia(chunk)
			}
		case "mark":
			// The mark follows the last clip of a reply: send the tail first so the caller hears all of it.
			if tail := b.out.flush(); tail != nil {
				b.sendMedia(tail)
			}
			if m.Mark != nil {
				b.toExotel(outMark{Event: "mark", StreamSID: b.streamSID, Mark: markBody{Name: m.Mark.Name}})
			}
		case "clear":
			b.out.reset()
			b.toExotel(outClear{Event: "clear", StreamSID: b.streamSID})
		}
	}
}

func (b *bridge) sendMedia(pcm []byte) {
	b.toExotel(outMedia{Event: "media", StreamSID: b.streamSID, Media: outMediaBody{Payload: base64.StdEncoding.EncodeToString(pcm)}})
}

func (b *bridge) toExotel(v any) {
	b.exMu.Lock()
	defer b.exMu.Unlock()
	if err := b.ex.WriteJSON(v); err != nil {
		log.Printf("[exotel %s] write: %v", b.callID, err)
	}
}

func (b *bridge) toEngine(v any) {
	b.engMu.Lock()
	defer b.engMu.Unlock()
	if b.eng == nil {
		return
	}
	if err := b.eng.WriteJSON(v); err != nil {
		log.Printf("[exotel %s] engine write: %v", b.callID, err)
	}
}

// close ends both legs. Closing Exotel's socket lets its flow move on to the next step (hang up).
func (b *bridge) close() {
	b.closeOnce.Do(func() {
		close(b.done)
		if b.eng != nil {
			_ = b.eng.Close()
		}
		_ = b.ex.Close()
	})
}
