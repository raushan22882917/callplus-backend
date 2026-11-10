package deepgram

import (
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/gorilla/websocket"

	"callcenter/realtime/internal/sarvam"
)

// serve starts a fake Deepgram that sends the given messages, then waits for the client to hang up.
func serve(t *testing.T, messages []string, gotQuery *string, gotAuth *string, audio *[]byte) *Stream {
	t.Helper()
	up := websocket.Upgrader{}
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if gotQuery != nil {
			*gotQuery = r.URL.RawQuery
		}
		if gotAuth != nil {
			*gotAuth = r.Header.Get("Authorization")
		}
		c, err := up.Upgrade(w, r, nil)
		if err != nil {
			return
		}
		defer c.Close()
		if audio != nil {
			if kind, data, err := c.ReadMessage(); err == nil && kind == websocket.BinaryMessage {
				*audio = data
			}
		}
		for _, m := range messages {
			_ = c.WriteMessage(websocket.TextMessage, []byte(m))
		}
		for {
			if _, _, err := c.ReadMessage(); err != nil {
				return
			}
		}
	}))
	t.Cleanup(srv.Close)
	old := baseURL
	baseURL = "ws" + strings.TrimPrefix(srv.URL, "http")
	t.Cleanup(func() { baseURL = old })
	s, err := Dial("test-key", "es", 800)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = s.Close() })
	return s
}

func next(t *testing.T, s *Stream) sarvam.Event {
	t.Helper()
	ev, err := s.Next()
	if err != nil {
		t.Fatal(err)
	}
	return ev
}

func TestDialSendsKeyAndTelephonyAudioSettings(t *testing.T) {
	var query, auth string
	var audio []byte
	s := serve(t, nil, &query, &auth, &audio)
	if err := s.SendAudio("AQID"); err != nil { // base64 of 0x01 0x02 0x03
		t.Fatal(err)
	}
	// The fake reads the first audio frame before replying; give it a round trip.
	_ = s.conn.WriteMessage(websocket.TextMessage, []byte("{}"))
	if auth != "Token test-key" {
		t.Errorf("auth header = %q", auth)
	}
	for _, want := range []string{"encoding=mulaw", "sample_rate=8000", "language=es", "model=nova-3", "interim_results=true", "endpointing=800"} {
		if !strings.Contains(query, want) {
			t.Errorf("query %q is missing %q", query, want)
		}
	}
}

func TestPiecesAreJoinedIntoOneFinalTranscript(t *testing.T) {
	s := serve(t, []string{
		`{"type":"SpeechStarted"}`,
		`{"type":"Results","is_final":false,"channel":{"alternatives":[{"transcript":"hola"}]}}`,
		`{"type":"Results","is_final":true,"speech_final":false,"channel":{"alternatives":[{"transcript":"hola buenos"}]}}`,
		`{"type":"Results","is_final":false,"channel":{"alternatives":[{"transcript":"dias"}]}}`,
		`{"type":"Results","is_final":true,"speech_final":true,"channel":{"alternatives":[{"transcript":"dias","languages":["es"]}]}}`,
	}, nil, nil, nil)

	if ev := next(t, s); ev.Kind != sarvam.EventSpeechStart {
		t.Fatalf("want speech start, got %v", ev)
	}
	if ev := next(t, s); ev.Kind != sarvam.EventPartial || ev.Text != "hola" {
		t.Fatalf("want partial hola, got %+v", ev)
	}
	if ev := next(t, s); ev.Kind != sarvam.EventIgnored { // a finalised piece waits for the end of the sentence
		t.Fatalf("a finalised piece should not be a transcript yet, got %+v", ev)
	}
	if ev := next(t, s); ev.Kind != sarvam.EventPartial || ev.Text != "hola buenos dias" {
		t.Fatalf("partial should show everything so far, got %+v", ev)
	}
	ev := next(t, s)
	if ev.Kind != sarvam.EventFinal || ev.Text != "hola buenos dias" || ev.Language != "es" {
		t.Fatalf("want one final transcript, got %+v", ev)
	}
}

func TestUtteranceEndHandsOverWhatWasHeard(t *testing.T) {
	s := serve(t, []string{
		`{"type":"Results","is_final":true,"speech_final":false,"channel":{"alternatives":[{"transcript":"call me back"}]}}`,
		`{"type":"UtteranceEnd"}`,
		`{"type":"UtteranceEnd"}`,
	}, nil, nil, nil)
	if ev := next(t, s); ev.Kind != sarvam.EventIgnored {
		t.Fatalf("got %+v", ev)
	}
	if ev := next(t, s); ev.Kind != sarvam.EventFinal || ev.Text != "call me back" {
		t.Fatalf("utterance end should flush the sentence, got %+v", ev)
	}
	if ev := next(t, s); ev.Kind != sarvam.EventSpeechEnd { // nothing left to flush
		t.Fatalf("got %+v", ev)
	}
}

func TestProviderErrorsAreReturned(t *testing.T) {
	s := serve(t, []string{`{"type":"Error","description":"bad audio","err_code":"X1"}`}, nil, nil, nil)
	if _, err := s.Next(); err == nil || !strings.Contains(err.Error(), "bad audio") {
		t.Fatalf("want the provider's error, got %v", err)
	}
}

func TestLanguageMapping(t *testing.T) {
	cases := map[string]string{"": "multi", "es": "es", "es-MX": "es-419", "en-US": "en-US"}
	for in, want := range cases {
		if got := Language(in); got != want {
			t.Errorf("Language(%q) = %q, want %q", in, got, want)
		}
	}
}
