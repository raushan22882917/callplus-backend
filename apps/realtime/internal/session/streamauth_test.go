package session

import (
	"net/http"
	"net/http/httptest"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"github.com/gorilla/websocket"

	"callcenter/realtime/internal/twilio"
)

// The signature is computed by the API in Python (hmac sha256 over "stream:<id>:<exp>", see
// app/services/stream_auth.py); this vector keeps the two sides in step.
const (
	stKey = "unit-test-internal-token-123456"
	stID  = "11111111-2222-3333-4444-555555555555"
	stExp = int64(1700000600)
	stSig = "77c12312149002b2a1419be4d8875dcb56e85797680f36e1d51926c99eb28c7e"
)

func TestVerifyStreamAcceptsTheApisSignature(t *testing.T) {
	if !VerifyStream(stKey, stID, stExp, stSig, time.Unix(1700000000, 0)) {
		t.Fatal("a valid signature was rejected")
	}
}

func TestVerifyStreamRejectsTamperingAndExpiry(t *testing.T) {
	now := time.Unix(1700000000, 0)
	cases := map[string]bool{
		"other call":       VerifyStream(stKey, "99999999-2222-3333-4444-555555555555", stExp, stSig, now),
		"later exp":        VerifyStream(stKey, stID, stExp+1, stSig, now),
		"wrong key":        VerifyStream("another-key-another-key-123456", stID, stExp, stSig, now),
		"empty sig":        VerifyStream(stKey, stID, stExp, "", now),
		"expired":          VerifyStream(stKey, stID, stExp, stSig, time.Unix(1700000601, 0)),
		"no call id":       VerifyStream(stKey, "", stExp, stSig, now),
		"web-call sig":     VerifyStream(stKey, stID, stExp, webCallMAC(stKey, stID, stExp), now),
		"stream sig as wc": VerifyWebCall(stKey, stID, stExp, stSig, now),
	}
	for name, ok := range cases {
		if ok {
			t.Errorf("%s: should have been rejected", name)
		}
	}
}

func TestSignStreamIsAcceptedByTheSession(t *testing.T) {
	s := &Session{cfg: Config{InternalToken: stKey}, callID: stID}
	if !s.streamAuthorized(SignStream(stKey, stID, time.Now())) {
		t.Fatal("the gateway's own signature was rejected")
	}
	if s.streamAuthorized(map[string]string{"callId": stID}) {
		t.Fatal("a start message with no signature was accepted")
	}
	if s.streamAuthorized(SignStream(stKey, "another-call", time.Now())) {
		t.Fatal("a signature for another call was accepted")
	}
	s.cfg.AllowUnsignedStreams = true
	if !s.streamAuthorized(nil) {
		t.Fatal("ALLOW_UNSIGNED_STREAMS should skip the check")
	}
}

// rejectedStream connects to a gateway session, sends the given frames and reports whether the gateway
// closed the socket, and how many times the session called the backend.
func rejectedStream(t *testing.T, frames ...string) (closed bool, backendCalls int32) {
	t.Helper()
	var calls atomic.Int32
	backend := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls.Add(1)
		http.NotFound(w, r)
	}))
	defer backend.Close()

	done := make(chan struct{})
	up := websocket.Upgrader{}
	gateway := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		conn, err := up.Upgrade(w, r, nil)
		if err != nil {
			return
		}
		New(conn, Config{BackendBaseURL: backend.URL, InternalToken: stKey, SarvamAPIKey: "unused"}).Run()
		close(done)
	}))
	defer gateway.Close()

	client, _, err := websocket.DefaultDialer.Dial("ws"+strings.TrimPrefix(gateway.URL, "http"), nil)
	if err != nil {
		t.Fatalf("dial: %v", err)
	}
	defer client.Close()
	for _, frame := range frames {
		if err := client.WriteMessage(websocket.TextMessage, []byte(frame)); err != nil {
			t.Fatalf("write: %v", err)
		}
	}
	select {
	case <-done:
		closed = true
	case <-time.After(3 * time.Second):
	}
	return closed, calls.Load()
}

func TestStartWithoutABodyClosesTheStreamInsteadOfCrashing(t *testing.T) {
	closed, calls := rejectedStream(t, `{"event":"connected"}`, `{"event":"start"}`)
	if !closed {
		t.Fatal("the stream should have been closed")
	}
	if calls != 0 {
		t.Fatalf("nothing should reach the backend, got %d calls", calls)
	}
}

func TestUnsignedStreamIsClosedBeforeAnythingIsSpent(t *testing.T) {
	closed, calls := rejectedStream(t,
		`{"event":"start","start":{"streamSid":"MZ1","customParameters":{"callId":"`+stID+`"}}}`)
	if !closed {
		t.Fatal("an unsigned stream should have been closed")
	}
	if calls != 0 {
		t.Fatalf("an unsigned stream must not fetch a call config or end a call, got %d backend calls", calls)
	}
}

func TestRepeatedStartIsIgnored(t *testing.T) {
	s := &Session{started: true, callID: stID}
	if !s.handleStart(&twilio.StartPayload{CustomParameters: map[string]string{"callId": "other"}}) {
		t.Fatal("a repeated start should be ignored, not end the call")
	}
	if s.callID != stID {
		t.Fatalf("a repeated start changed the call id to %q", s.callID)
	}
}

func TestRecoverPanicEndsOnlyThatCall(t *testing.T) {
	closed, _ := func() (bool, int32) {
		done := make(chan struct{})
		up := websocket.Upgrader{}
		gateway := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			conn, err := up.Upgrade(w, r, nil)
			if err != nil {
				return
			}
			s := New(conn, Config{})
			func() {
				defer s.recoverPanic("test")
				var start *twilio.StartPayload
				_ = start.StreamSID // the bug this guards against
			}()
			_, _, err = conn.ReadMessage()
			if err != nil {
				close(done)
			}
		}))
		defer gateway.Close()
		client, _, err := websocket.DefaultDialer.Dial("ws"+strings.TrimPrefix(gateway.URL, "http"), nil)
		if err != nil {
			t.Fatalf("dial: %v", err)
		}
		defer client.Close()
		select {
		case <-done:
			return true, 0
		case <-time.After(3 * time.Second):
			return false, 0
		}
	}()
	if !closed {
		t.Fatal("a recovered panic should close that call's socket")
	}
}
