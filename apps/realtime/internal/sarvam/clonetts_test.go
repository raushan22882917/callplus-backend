package sarvam

import (
	"encoding/base64"
	"io"
	"mime"
	"mime/multipart"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

func TestLinearToMulawMatchesG711(t *testing.T) {
	// Reference values from the standard G.711 mu-law algorithm.
	cases := map[int16]byte{0: 0xff, 1: 0xff, -1: 0x7f, 100: 0xf2, 1000: 0xce, -1000: 0x4e, 8000: 0xa0, 32767: 0x80, -32768: 0x00}
	for in, want := range cases {
		if got := linearToMulaw(in); got != want {
			t.Errorf("linearToMulaw(%d) = %#x, want %#x", in, got, want)
		}
	}
}

func TestCloneVoiceIds(t *testing.T) {
	if !IsCloneVoice("svc-1234") || IsCloneVoice("priya") || IsCloneVoice("svc-") || IsCloneVoice("") {
		t.Fatal("clone voice detection is wrong")
	}
}

func TestCloneStreamAsksForPcmAndHandsOnMulaw(t *testing.T) {
	// 3 pieces' worth of 16-bit samples plus one stray byte that must be dropped: 0, 1000, -1000, 8000, repeated.
	var pcm []byte
	for i := 0; i < clonePiece*2+8; i++ {
		v := []int16{0, 1000, -1000, 8000}[(i/2)%4]
		if i%2 == 0 {
			pcm = append(pcm, byte(uint16(v)&0xff))
		} else {
			pcm = append(pcm, byte(uint16(v)>>8))
		}
	}
	pcm = append(pcm, 0x7f) // odd trailing byte

	var path, key string
	got := map[string]string{}
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		path, key = r.URL.Path, r.Header.Get("api-subscription-key")
		_, params, _ := mime.ParseMediaType(r.Header.Get("Content-Type"))
		mr := multipart.NewReader(r.Body, params["boundary"])
		for {
			p, err := mr.NextPart()
			if err != nil {
				break
			}
			b, _ := io.ReadAll(p)
			got[p.FormName()] = string(b)
		}
		_, _ = w.Write(pcm)
	}))
	defer srv.Close()
	old := cloneBaseURL
	cloneBaseURL = srv.URL
	defer func() { cloneBaseURL = old }()

	var out []byte
	n, err := NewCloneClient("secret").Stream("Namaste", "svc-abc", "hi-IN", func(b64 string) {
		piece, _ := base64.StdEncoding.DecodeString(b64)
		out = append(out, piece...)
	})
	if err != nil {
		t.Fatal(err)
	}
	if path != "/voices/clone/stream" || key != "secret" {
		t.Errorf("wrong request: %s key=%q", path, key)
	}
	if got["text"] != "Namaste" || got["voice_id"] != "svc-abc" || got["language_code"] != "hi-IN" ||
		got["output_audio_codec"] != "linear16" || got["speech_sample_rate"] != "8000" {
		t.Errorf("wrong form fields: %v", got)
	}
	if want := (len(pcm) - 1) / 2; len(out) != want { // the stray byte is dropped, one byte out per sample
		t.Fatalf("got %d mu-law bytes, want %d (pieces=%d)", len(out), want, n)
	}
	for i, want := range []byte{0xff, 0xce, 0x4e, 0xa0, 0xff, 0xce, 0x4e, 0xa0} {
		if out[i] != want {
			t.Errorf("sample %d = %#x, want %#x", i, out[i], want)
		}
	}
}

func TestCloneStreamWithoutLanguageSendsNoneAndReportsErrors(t *testing.T) {
	var fields []string
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_, params, _ := mime.ParseMediaType(r.Header.Get("Content-Type"))
		mr := multipart.NewReader(r.Body, params["boundary"])
		for {
			p, err := mr.NextPart()
			if err != nil {
				break
			}
			fields = append(fields, p.FormName())
		}
		w.WriteHeader(http.StatusForbidden)
		_, _ = w.Write([]byte(`{"error":{"message":"voice not in your workspace"}}`))
	}))
	defer srv.Close()
	old := cloneBaseURL
	cloneBaseURL = srv.URL
	defer func() { cloneBaseURL = old }()

	n, err := NewCloneClient("k").Stream("hi", "svc-x", "", func(string) { t.Fatal("no audio expected") })
	if err == nil || n != 0 || !strings.Contains(err.Error(), "403") {
		t.Fatalf("want a 403 error, got %v (%d)", err, n)
	}
	for _, f := range fields {
		if f == "language_code" {
			t.Error("language_code should be left out when unknown")
		}
	}
}
