package elevenlabs

import (
	"encoding/base64"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

func TestStreamAsksForPhoneAudioAndHandsItOnInPieces(t *testing.T) {
	audio := make([]byte, pieceBytes*2+500) // two full pieces and a short one
	for i := range audio {
		audio[i] = byte(i)
	}
	var path, query, key string
	var sent request
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		path, query, key = r.URL.Path, r.URL.RawQuery, r.Header.Get("xi-api-key")
		raw, _ := io.ReadAll(r.Body)
		_ = json.Unmarshal(raw, &sent)
		_, _ = w.Write(audio)
	}))
	defer srv.Close()
	old := baseURL
	baseURL = srv.URL
	defer func() { baseURL = old }()

	var got []byte
	n, err := NewClient("secret", "").Stream("Hola, buenas tardes", "voice123", func(b64 string) {
		piece, _ := base64.StdEncoding.DecodeString(b64)
		got = append(got, piece...)
	})
	if err != nil {
		t.Fatal(err)
	}
	if n != 3 || string(got) != string(audio) {
		t.Fatalf("pieces=%d, audio intact=%v", n, string(got) == string(audio))
	}
	if path != "/v1/text-to-speech/voice123/stream" || !strings.Contains(query, "output_format=ulaw_8000") {
		t.Errorf("wrong request: %s?%s", path, query)
	}
	if key != "secret" || sent.Text != "Hola, buenas tardes" || sent.ModelID != DefaultModel {
		t.Errorf("wrong key or body: %q %+v", key, sent)
	}
}

func TestStreamReportsProviderErrorsAndMissingVoice(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusUnauthorized)
		_, _ = w.Write([]byte(`{"detail":{"status":"invalid_api_key"}}`))
	}))
	defer srv.Close()
	old := baseURL
	baseURL = srv.URL
	defer func() { baseURL = old }()

	n, err := NewClient("bad", "").Stream("hi", "v", func(string) { t.Fatal("no audio expected") })
	if err == nil || n != 0 || !strings.Contains(err.Error(), "401") {
		t.Fatalf("want a 401 error, got %v (%d)", err, n)
	}
	if _, err := NewClient("k", "").Stream("hi", "", func(string) {}); err == nil {
		t.Fatal("a missing voice should be an error")
	}
}
