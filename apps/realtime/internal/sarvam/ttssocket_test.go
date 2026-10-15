package sarvam

import (
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"

	"github.com/gorilla/websocket"
)

// fakeSarvam speaks just enough of Sarvam's TTS socket protocol: a config
// message first, then text + flush -> audio chunks -> a "final" event.
type fakeSarvam struct {
	t        *testing.T
	mu       sync.Mutex
	dials    int
	configs  []map[string]any
	texts    []string
	dropNext bool // close the socket on the next text, simulating a stale connection
	failWith string
}

func (f *fakeSarvam) server() *httptest.Server {
	upgrader := websocket.Upgrader{}
	return httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("API-SUBSCRIPTION-KEY") != "key" {
			http.Error(w, "unauthorized", http.StatusUnauthorized)
			return
		}
		conn, err := upgrader.Upgrade(w, r, nil)
		if err != nil {
			return
		}
		f.mu.Lock()
		f.dials++
		f.mu.Unlock()
		go func() {
			defer conn.Close()
			for {
				var msg map[string]any
				if err := conn.ReadJSON(&msg); err != nil {
					return
				}
				switch msg["type"] {
				case "config":
					f.mu.Lock()
					f.configs = append(f.configs, msg["data"].(map[string]any))
					f.mu.Unlock()
				case "text":
					f.mu.Lock()
					f.texts = append(f.texts, msg["data"].(map[string]any)["text"].(string))
					drop, fail := f.dropNext, f.failWith
					f.dropNext = false
					f.mu.Unlock()
					if drop {
						return
					}
					if fail != "" {
						_ = conn.WriteJSON(map[string]any{"type": "error", "data": map[string]any{"message": fail}})
						return
					}
				case "flush":
					for _, chunk := range []string{"AAA=", "BBB=", "CCC="} {
						_ = conn.WriteJSON(map[string]any{"type": "audio", "data": map[string]any{"audio": chunk}})
					}
					_ = conn.WriteJSON(map[string]any{"type": "event", "data": map[string]any{"event_type": "final"}})
				}
			}
		}()
	}))
}

func wsURL(s *httptest.Server) string { return "ws" + strings.TrimPrefix(s.URL, "http") }

func TestStreamDeliversChunksInOrderOverOneConnection(t *testing.T) {
	fake := &fakeSarvam{t: t}
	server := fake.server()
	defer server.Close()
	socket := newTTSSocket(wsURL(server), "key", "hi-IN", "priya")
	defer socket.Close()

	for i := 0; i < 2; i++ {
		var got []string
		n, err := socket.Stream("नमस्ते।", func(c string) { got = append(got, c) })
		if err != nil || n != 3 || strings.Join(got, ",") != "AAA=,BBB=,CCC=" {
			t.Fatalf("sentence %d: n=%d err=%v got=%v", i, n, err, got)
		}
	}
	fake.mu.Lock()
	defer fake.mu.Unlock()
	if fake.dials != 1 || len(fake.texts) != 2 {
		t.Fatalf("one connection should serve both sentences: dials=%d texts=%v", fake.dials, fake.texts)
	}
	cfg := fake.configs[0]
	if cfg["speaker"] != "priya" || cfg["language_code"] != "hi-IN" || cfg["output_audio_codec"] != "mulaw" || cfg["speech_sample_rate"].(float64) != 8000 {
		t.Fatalf("bad config %v", cfg)
	}
}

func TestWarmOpensTheConnectionBeforeTheFirstSentence(t *testing.T) {
	fake := &fakeSarvam{t: t}
	server := fake.server()
	defer server.Close()
	socket := newTTSSocket(wsURL(server), "key", "en-IN", "")
	defer socket.Close()
	if err := socket.Warm(); err != nil {
		t.Fatal(err)
	}
	if _, err := socket.Stream("Hello there.", func(string) {}); err != nil {
		t.Fatal(err)
	}
	fake.mu.Lock()
	defer fake.mu.Unlock()
	if fake.dials != 1 {
		t.Fatalf("warm + stream should share one dial, got %d", fake.dials)
	}
	if _, has := fake.configs[0]["speaker"]; has {
		t.Fatal("an empty voice must be left out so Sarvam picks its default")
	}
}

func TestStaleConnectionIsRedialledBeforeAnyAudio(t *testing.T) {
	fake := &fakeSarvam{t: t}
	server := fake.server()
	defer server.Close()
	socket := newTTSSocket(wsURL(server), "key", "en-IN", "priya")
	defer socket.Close()
	if _, err := socket.Stream("First.", func(string) {}); err != nil {
		t.Fatal(err)
	}
	fake.mu.Lock()
	fake.dropNext = true
	fake.mu.Unlock()
	var got []string
	n, err := socket.Stream("Second one here.", func(c string) { got = append(got, c) })
	if err != nil || n != 3 {
		t.Fatalf("the dropped socket must be replaced transparently: n=%d err=%v", n, err)
	}
	fake.mu.Lock()
	defer fake.mu.Unlock()
	if fake.dials != 2 {
		t.Fatalf("expected a re-dial, got %d dials", fake.dials)
	}
}

func TestServerErrorAndBadKeyAreReported(t *testing.T) {
	fake := &fakeSarvam{t: t, failWith: "quota exceeded"}
	server := fake.server()
	defer server.Close()
	socket := newTTSSocket(wsURL(server), "key", "en-IN", "")
	defer socket.Close()
	n, err := socket.Stream("Hello.", func(string) {})
	if err == nil || n != 0 || !strings.Contains(err.Error(), "quota exceeded") {
		t.Fatalf("got n=%d err=%v", n, err)
	}

	bad := newTTSSocket(wsURL(server), "wrong", "en-IN", "")
	defer bad.Close()
	if _, err := bad.Stream("Hello.", func(string) {}); err == nil {
		t.Fatal("a rejected handshake must fail")
	}
	closed := newTTSSocket(wsURL(server), "key", "en-IN", "")
	closed.Close()
	if _, err := closed.Stream("Hello.", func(string) {}); err == nil {
		t.Fatal("a closed socket must refuse work")
	}
}
