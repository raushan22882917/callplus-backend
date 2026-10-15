package backend

import (
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

func serve(t *testing.T, status int, body string) *Client {
	t.Helper()
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/calls/call-1/turn/stream" || r.Header.Get("X-Internal-Token") != "tok" {
			t.Errorf("unexpected request %s token=%q", r.URL.Path, r.Header.Get("X-Internal-Token"))
		}
		w.WriteHeader(status)
		_, _ = w.Write([]byte(body))
	}))
	t.Cleanup(server.Close)
	return NewClient(server.URL, "tok")
}

func TestTurnStreamDeliversSentencesThenDone(t *testing.T) {
	body := `{"type":"sentence","text":"Sure, I can help."}` + "\n" +
		`{"type":"sentence","text":"नमस्ते। What time?"}` + "\n" +
		`{"type":"done","reply_text":"Sure, I can help. What time?","action":"transfer","reason":"asked","reply_message_id":"m1"}` + "\n"
	var got []string
	result, streamed, err := serve(t, 200, body).TurnStream("call-1", "hi", func(s string) { got = append(got, s) })
	if err != nil || streamed != 2 || len(got) != 2 || got[1] != "नमस्ते। What time?" {
		t.Fatalf("err=%v streamed=%d got=%v", err, streamed, got)
	}
	if result.Action != "transfer" || result.ReplyMessageID != "m1" || result.ReplyText != "Sure, I can help. What time?" {
		t.Fatalf("bad result %+v", result)
	}
	if !strings.Contains(string(result.Raw), `"type":"done"`) {
		t.Fatalf("Raw should carry the done line, got %s", result.Raw)
	}
}

func TestTurnStreamWithOnlyDoneStreamsNothing(t *testing.T) {
	body := `{"type":"done","reply_text":"Time is up.","action":"hangup","reason":"max_duration_exceeded"}` + "\n"
	result, streamed, err := serve(t, 200, body).TurnStream("call-1", "hi", func(string) { t.Fatal("no sentence expected") })
	if err != nil || streamed != 0 || result.Action != "hangup" || result.ReplyText != "Time is up." {
		t.Fatalf("result=%+v streamed=%d err=%v", result, streamed, err)
	}
}

func TestTurnStreamErrors(t *testing.T) {
	noop := func(string) {}
	if _, _, err := serve(t, 404, "not found").TurnStream("call-1", "hi", noop); err == nil || !strings.Contains(err.Error(), "404") {
		t.Fatalf("a 404 (old backend) must be an error, got %v", err)
	}
	if _, n, err := serve(t, 200, `{"type":"error","status":500,"detail":"turn failed"}`+"\n").TurnStream("call-1", "hi", noop); err == nil || n != 0 {
		t.Fatalf("an error line must fail the turn, got n=%d err=%v", n, err)
	}
	body := `{"type":"sentence","text":"Half a reply."}` + "\n"
	if _, n, err := serve(t, 200, body).TurnStream("call-1", "hi", noop); err == nil || n != 1 {
		t.Fatalf("a stream that ends without done must fail after n=1, got n=%d err=%v", n, err)
	}
	if _, _, err := serve(t, 200, "not json\n").TurnStream("call-1", "hi", noop); err == nil {
		t.Fatal("garbage must fail")
	}
}

func TestOnlyPhoneCallsCanStream(t *testing.T) {
	if !NewClient("http://x", "t").CanStream() {
		t.Fatal("calls can stream")
	}
	if NewTestClient("http://x", "t", "s").CanStream() {
		t.Fatal("browser test sessions must use the buffered path")
	}
}
