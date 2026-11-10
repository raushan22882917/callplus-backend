// Command gateway is the realtime plane: a Go WebSocket server that Twilio
// Media Streams connects to for each call. See internal/session for the
// per-call orchestration and app/api/v1/internal.py (the Python side) for
// the "AI Engine" this gateway calls into.
package main

import (
	"log"
	"net/http"
	"os"
	"strconv"
	"strings"
	"time"

	"github.com/gorilla/websocket"

	"callcenter/realtime/internal/exotel"
	"callcenter/realtime/internal/session"
)

var upgrader = websocket.Upgrader{
	// Twilio doesn't send an Origin header matching this service's own
	// origin (there isn't a browser involved), so the default same-origin
	// check would reject every connection — allow all origins here and
	// rely on the callId/tenantId the backend already validates per call.
	CheckOrigin: func(r *http.Request) bool { return true },
}

func main() {
	cfg := session.Config{
		SarvamAPIKey:    requireEnv("SARVAM_API_KEY"),
		BackendBaseURL:  requireEnv("BACKEND_BASE_URL"),
		InternalToken:   requireEnv("INTERNAL_SERVICE_TOKEN"),
		DefaultLanguage: getEnv("DEFAULT_LANGUAGE", "en-IN"),
		// Speak each sentence as the LLM writes it. Set STREAM_REPLIES=false to go
		// back to waiting for the whole reply (falls back by itself on any error).
		// Persistent Sarvam text-to-speech WebSocket per call: ~0.25s to first audio
		// per sentence instead of ~1s for the REST call. TTS_WEBSOCKET=false forces REST.
		TTSWebSocket:  getEnv("TTS_WEBSOCKET", "true") != "false",
		StreamReplies: getEnv("STREAM_REPLIES", "true") != "false",
	}
	port := getEnv("PORT", "9000")

	http.HandleFunc("/healthz", func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte("ok"))
	})

	http.HandleFunc("/ws/twilio", func(w http.ResponseWriter, r *http.Request) {
		conn, err := upgrader.Upgrade(w, r, nil)
		if err != nil {
			log.Printf("websocket upgrade failed: %v", err)
			return
		}
		sess := session.New(conn, cfg)
		go sess.Run()
	})

	// Exotel Voicebot: an Indian carrier streaming 16-bit audio. The bridge translates it to the
	// shape the call engine already speaks and hands it to /ws/twilio on this same server.
	exotelCfg := exotel.Config{
		EngineURL: "ws://127.0.0.1:" + port + "/ws/twilio",
		Start:     exotel.HTTPStart(cfg.BackendBaseURL, cfg.InternalToken),
	}
	http.HandleFunc("/ws/exotel", func(w http.ResponseWriter, r *http.Request) {
		key := r.URL.Query().Get("k")
		if key == "" {
			http.Error(w, "missing key", http.StatusUnauthorized)
			return
		}
		conn, err := upgrader.Upgrade(w, r, nil)
		if err != nil {
			log.Printf("websocket upgrade failed: %v", err)
			return
		}
		go exotel.Handle(conn, key, exotelCfg)
	})

	// Flow Studio "Test Audio": a browser speaking the same Media Streams protocol
	// as Twilio. The backend checks the session ticket when the call starts.
	http.HandleFunc("/ws/browser-test", func(w http.ResponseWriter, r *http.Request) {
		sessionID := r.URL.Query().Get("session")
		token := r.URL.Query().Get("token")
		if !strings.HasPrefix(sessionID, "test_") || token == "" {
			http.Error(w, "missing test session", http.StatusBadRequest)
			return
		}
		conn, err := upgrader.Upgrade(w, r, nil)
		if err != nil {
			log.Printf("websocket upgrade failed: %v", err)
			return
		}
		sess := session.NewBrowserTest(conn, cfg, sessionID, token)
		go sess.Run()
	})

	// A visitor calling from a web page. The API created the call and signed this address for a few
	// minutes; anything else is turned away before the WebSocket opens.
	http.HandleFunc("/ws/web-call", func(w http.ResponseWriter, r *http.Request) {
		q := r.URL.Query()
		callID := q.Get("call")
		exp, err := strconv.ParseInt(q.Get("exp"), 10, 64)
		if err != nil || !session.VerifyWebCall(cfg.InternalToken, callID, exp, q.Get("sig"), time.Now()) {
			http.Error(w, "this call address is invalid or has expired", http.StatusForbidden)
			return
		}
		conn, err := upgrader.Upgrade(w, r, nil)
		if err != nil {
			log.Printf("websocket upgrade failed: %v", err)
			return
		}
		sess := session.NewWebCall(conn, cfg, callID)
		go sess.Run()
	})

	log.Printf("realtime gateway listening on :%s", port)
	if err := http.ListenAndServe(":"+port, nil); err != nil {
		log.Fatalf("server failed: %v", err)
	}
}

func requireEnv(key string) string {
	value := os.Getenv(key)
	if value == "" {
		log.Fatalf("missing required environment variable: %s", key)
	}
	return value
}

func getEnv(key, fallback string) string {
	if value := os.Getenv(key); value != "" {
		return value
	}
	return fallback
}
