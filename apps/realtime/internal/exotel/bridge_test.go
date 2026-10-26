package exotel

import (
	"encoding/base64"
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/gorilla/websocket"
)

var up = websocket.Upgrader{CheckOrigin: func(*http.Request) bool { return true }}

func TestMulawRoundTripStaysClose(t *testing.T) {
	for _, s := range []int16{0, 100, -100, 1000, -1000, 8000, -8000, 30000, -30000} {
		back := int(mulawDecode(mulawEncode(s)))
		diff := back - int(s)
		if diff < 0 {
			diff = -diff
		}
		// mu-law keeps ~13 bits of precision; the error grows with loudness.
		if diff > int(abs(int(s))/16)+40 {
			t.Fatalf("sample %d came back as %d", s, back)
		}
	}
}

func abs(v int) int {
	if v < 0 {
		return -v
	}
	return v
}

func TestChunkerSizes(t *testing.T) {
	var c chunker
	if got := c.add(make([]byte, 1000)); len(got) != 0 {
		t.Fatalf("too small to send yet, got %d chunks", len(got))
	}
	got := c.add(make([]byte, 2500)) // 3500 buffered
	if len(got) != 1 || len(got[0])%chunkUnit != 0 || len(got[0]) < chunkMin {
		t.Fatalf("bad chunk sizes: %v", len(got))
	}
	tail := c.flush()
	if tail == nil || len(tail)%chunkUnit != 0 || len(tail) < chunkMin {
		t.Fatalf("flush must pad to a valid size, got %d", len(tail))
	}
	if c.flush() != nil {
		t.Fatal("nothing should be left")
	}
	c.add(make([]byte, 100))
	c.reset()
	if c.flush() != nil {
		t.Fatal("reset must drop unsent audio")
	}
}

// fakeEngine stands in for the call engine: records what the bridge sends, can send back speech.
type fakeEngine struct {
	srv  *httptest.Server
	got  chan map[string]any
	conn chan *websocket.Conn
}

func newFakeEngine(t *testing.T) *fakeEngine {
	e := &fakeEngine{got: make(chan map[string]any, 50), conn: make(chan *websocket.Conn, 1)}
	e.srv = httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		c, err := up.Upgrade(w, r, nil)
		if err != nil {
			return
		}
		e.conn <- c
		for {
			var m map[string]any
			if err := c.ReadJSON(&m); err != nil {
				return
			}
			e.got <- m
		}
	}))
	t.Cleanup(e.srv.Close)
	return e
}

func (e *fakeEngine) url() string { return "ws" + strings.TrimPrefix(e.srv.URL, "http") }

func startBridge(t *testing.T, e *fakeEngine, start StartFunc) *websocket.Conn {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		c, err := up.Upgrade(w, r, nil)
		if err != nil {
			return
		}
		Handle(c, r.URL.Query().Get("k"), Config{EngineURL: e.url(), Start: start})
	}))
	t.Cleanup(srv.Close)
	c, _, err := websocket.DefaultDialer.Dial("ws"+strings.TrimPrefix(srv.URL, "http")+"?k=SECRET", nil)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = c.Close() })
	return c
}

func next(t *testing.T, ch chan map[string]any) map[string]any {
	select {
	case m := <-ch:
		return m
	case <-time.After(3 * time.Second):
		t.Fatal("timed out waiting for a message")
		return nil
	}
}

func TestCallStartsAndAudioFlowsBothWays(t *testing.T) {
	e := newFakeEngine(t)
	var gotKey, gotTo string
	ex := startBridge(t, e, func(key, sid, from, to string) (*CallInfo, error) {
		gotKey, gotTo = key, to
		return &CallInfo{CallID: "call-1", TenantID: "tenant-1"}, nil
	})

	_ = ex.WriteJSON(map[string]any{"event": "connected"})
	_ = ex.WriteJSON(map[string]any{"event": "start", "stream_sid": "S1", "start": map[string]any{
		"stream_sid": "S1", "call_sid": "C1", "account_sid": "acct", "from": "+919800000001", "to": "+918000000002",
	}})

	start := next(t, e.got)
	if start["event"] != "start" {
		t.Fatalf("engine should get start first, got %v", start["event"])
	}
	params := start["start"].(map[string]any)["customParameters"].(map[string]any)
	if params["callId"] != "call-1" || params["tenantId"] != "tenant-1" {
		t.Fatalf("engine must be told which call this is: %v", params)
	}
	if gotKey != "SECRET" || gotTo != "+918000000002" {
		t.Fatalf("backend asked with key=%q to=%q", gotKey, gotTo)
	}

	// Caller audio: 160 samples of 16-bit PCM in, 160 mu-law bytes out.
	pcm := make([]byte, 320)
	_ = ex.WriteJSON(map[string]any{"event": "media", "stream_sid": "S1", "media": map[string]any{"payload": base64.StdEncoding.EncodeToString(pcm)}})
	m := next(t, e.got)
	payload, _ := base64.StdEncoding.DecodeString(m["media"].(map[string]any)["payload"].(string))
	if m["event"] != "media" || len(payload) != 160 {
		t.Fatalf("expected 160 mu-law bytes, got %d (%v)", len(payload), m["event"])
	}

	// Agent speech: 4000 mu-law bytes (0.5s) then the end-of-reply mark.
	eng := <-e.conn
	_ = eng.WriteJSON(map[string]any{"event": "media", "streamSid": "S1", "media": map[string]any{"payload": base64.StdEncoding.EncodeToString(make([]byte, 4000))}})
	_ = eng.WriteJSON(map[string]any{"event": "mark", "streamSid": "S1", "mark": map[string]any{"name": "end-1"}})

	total, sawMark := 0, false
	for !sawMark {
		_ = ex.SetReadDeadline(time.Now().Add(3 * time.Second))
		var out map[string]any
		if err := ex.ReadJSON(&out); err != nil {
			t.Fatal(err)
		}
		switch out["event"] {
		case "media":
			raw, _ := base64.StdEncoding.DecodeString(out["media"].(map[string]any)["payload"].(string))
			if len(raw)%chunkUnit != 0 || len(raw) < chunkMin {
				t.Fatalf("bad chunk size %d", len(raw))
			}
			total += len(raw)
			if out["stream_sid"] != "S1" {
				t.Fatalf("wrong stream id %v", out["stream_sid"])
			}
		case "mark":
			sawMark = out["mark"].(map[string]any)["name"] == "end-1"
		}
	}
	if total < 8000 { // 4000 mu-law bytes become 8000 bytes of 16-bit audio
		t.Fatalf("caller should hear all of the reply, got %d bytes", total)
	}

	// Barge-in: the engine's clear reaches Exotel.
	_ = eng.WriteJSON(map[string]any{"event": "clear", "streamSid": "S1"})
	_ = ex.SetReadDeadline(time.Now().Add(3 * time.Second))
	var out map[string]any
	if err := ex.ReadJSON(&out); err != nil || out["event"] != "clear" {
		t.Fatalf("expected clear, got %v err=%v", out, err)
	}
}

func TestRefusedCallClosesTheSocket(t *testing.T) {
	e := newFakeEngine(t)
	ex := startBridge(t, e, func(string, string, string, string) (*CallInfo, error) { return nil, errors.New("unknown number") })
	_ = ex.WriteJSON(map[string]any{"event": "start", "start": map[string]any{"stream_sid": "S", "call_sid": "C", "to": "+910"}})
	_ = ex.SetReadDeadline(time.Now().Add(3 * time.Second))
	if _, _, err := ex.ReadMessage(); err == nil {
		t.Fatal("a refused call must be disconnected")
	}
	select {
	case m := <-e.got:
		t.Fatalf("engine must not hear about a refused call: %v", m)
	default:
	}
}

func TestEngineHangupClosesExotel(t *testing.T) {
	e := newFakeEngine(t)
	ex := startBridge(t, e, func(string, string, string, string) (*CallInfo, error) { return &CallInfo{CallID: "c"}, nil })
	_ = ex.WriteJSON(map[string]any{"event": "start", "start": map[string]any{"stream_sid": "S", "call_sid": "C"}})
	next(t, e.got)
	eng := <-e.conn
	_ = eng.Close() // the AI ended the call
	_ = ex.SetReadDeadline(time.Now().Add(3 * time.Second))
	for {
		if _, _, err := ex.ReadMessage(); err != nil {
			return
		}
	}
}

func TestStopFromExotelTellsEngine(t *testing.T) {
	e := newFakeEngine(t)
	ex := startBridge(t, e, func(string, string, string, string) (*CallInfo, error) { return &CallInfo{CallID: "c"}, nil })
	_ = ex.WriteJSON(map[string]any{"event": "start", "start": map[string]any{"stream_sid": "S", "call_sid": "C"}})
	next(t, e.got)
	b, _ := json.Marshal(map[string]any{"event": "stop", "stop": map[string]any{"reason": "callended"}})
	_ = ex.WriteMessage(websocket.TextMessage, b)
	if m := next(t, e.got); m["event"] != "stop" {
		t.Fatalf("engine should hear stop, got %v", m["event"])
	}
}
