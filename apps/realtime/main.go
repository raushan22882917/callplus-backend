// Command gateway is the realtime plane: a Go WebSocket server that Twilio
// Media Streams connects to for each call. See internal/session for the
// per-call orchestration and app/api/v1/internal.py (the Python side) for
// the "AI Engine" this gateway calls into.
package main

import (
	"fmt"
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
	// check would reject every connection. Allow all origins here; each
	// endpoint proves the caller another way (a signed address, or the
	// signature in a carrier's start message, see session/streamauth.go).
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
		// Carrier streams must carry the API's signature for their call. ALLOW_UNSIGNED_STREAMS=true
		// turns that off for local development and load tests; never set it in production.
		AllowUnsignedStreams: getEnv("ALLOW_UNSIGNED_STREAMS", "false") == "true",
	}
	if cfg.AllowUnsignedStreams {
		log.Printf("WARNING: ALLOW_UNSIGNED_STREAMS is on: /ws/twilio accepts any caller")
	}
	port := getEnv("PORT", "9000")
	maxSessions, _ := strconv.Atoi(getEnv("MAX_SESSIONS", "0"))
	cap := newCapacity(maxSessions)

	http.HandleFunc("/healthz", func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte("ok"))
	})

	// Live numbers for a dashboard or an autoscaler: calls now, the busiest moment, calls turned away.
	http.HandleFunc("/statz", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_, _ = fmt.Fprintf(w, `{"active":%d,"peak":%d,"denied":%d,"max":%d}`,
			cap.active.Load(), cap.peak.Load(), cap.denied.Load(), cap.max)
	})

	// busy turns a call away with a 503 before the WebSocket opens, so the carrier or caller can retry
	// or fail over instead of joining a gateway that is already full.
	busy := func(w http.ResponseWriter) {
		w.Header().Set("Retry-After", "5")
		http.Error(w, "gateway at capacity", http.StatusServiceUnavailable)
	}

	http.HandleFunc("/ws/twilio", func(w http.ResponseWriter, r *http.Request) {
		if !cap.acquire() {
			busy(w)
			return
		}
		conn, err := upgrader.Upgrade(w, r, nil)
		if err != nil {
			cap.release()
			log.Printf("websocket upgrade failed: %v", err)
			return
		}
		sess := session.New(conn, cfg)
		go func() { defer cap.release(); sess.Run() }()
	})

	// Exotel Voicebot: an Indian carrier streaming 16-bit audio. The bridge translates it to the
	// shape the call engine already speaks and hands it to /ws/twilio on this same server.
	exotelCfg := exotel.Config{
		EngineURL: "ws://127.0.0.1:" + port + "/ws/twilio",
		Start:     exotel.HTTPStart(cfg.BackendBaseURL, cfg.InternalToken),
		Sign: func(callID string) map[string]string {
			return session.SignStream(cfg.InternalToken, callID, time.Now())
		},
	}
	http.HandleFunc("/ws/exotel", func(w http.ResponseWriter, r *http.Request) {
		key := r.URL.Query().Get("k")
		if key == "" {
			http.Error(w, "missing key", http.StatusUnauthorized)
			return
		}
		if cap.full() { // counted once the bridge reaches /ws/twilio
			busy(w)
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
		if !cap.acquire() {
			busy(w)
			return
		}
		conn, err := upgrader.Upgrade(w, r, nil)
		if err != nil {
			cap.release()
			log.Printf("websocket upgrade failed: %v", err)
			return
		}
		sess := session.NewBrowserTest(conn, cfg, sessionID, token)
		go func() { defer cap.release(); sess.Run() }()
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
		if !cap.acquire() {
			busy(w)
			return
		}
		conn, err := upgrader.Upgrade(w, r, nil)
		if err != nil {
			cap.release()
			log.Printf("websocket upgrade failed: %v", err)
			return
		}
		sess := session.NewWebCall(conn, cfg, callID)
		go func() { defer cap.release(); sess.Run() }()
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
