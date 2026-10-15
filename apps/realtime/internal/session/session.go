// Package session orchestrates one phone call: greet the caller, read
// Twilio Media Stream events, pipe audio to Sarvam STT, hand finished
// transcripts to the backend's AI Engine, speak the reply back via Sarvam
// TTS, and enforce the call's owner-configured policy — barge-in, a
// no-response retry limit, and a max call duration.
package session

import (
	"encoding/json"
	"fmt"
	"log"
	"sync"
	"time"

	"github.com/gorilla/websocket"

	"callcenter/realtime/internal/backend"
	"callcenter/realtime/internal/sarvam"
	"callcenter/realtime/internal/twilio"
)

type Config struct {
	// SarvamAPIKey and DefaultLanguage are fallbacks, used only if the
	// per-call config fetch from the backend (GetCallConfig) fails — the
	// backend resolves the actual key/language/policy per-tenant on every
	// call (see app/integrations/credentials.py), since more than one
	// tenant uses this same gateway.
	SarvamAPIKey    string
	BackendBaseURL  string
	InternalToken   string
	DefaultLanguage string
}

type Session struct {
	cfg     Config
	conn    *websocket.Conn
	writeMu sync.Mutex

	// browserTest: a Flow Studio "Test Audio" call from a browser that speaks the
	// Twilio Media Streams protocol. Everything below runs exactly as for a phone
	// call; the session additionally sends live captions and each turn's test
	// timeline to the browser. testSessionID is fixed by the URL, not the client.
	browserTest   bool
	testSessionID string

	streamSID string
	callID    string
	startedAt time.Time
	language  string
	voice     string

	backendClient *backend.Client
	callConfig    *backend.CallConfig
	stt           *sarvam.STTStream
	tts           *sarvam.TTSClient

	// Playback state. The agent counts as "speaking" until speakingUntil —
	// the estimated end of everything already sent to Twilio — or until
	// Twilio acks the latest mark, whichever comes first. (Flipping a plain
	// bool back to false as soon as the audio was *sent* made barge-in and
	// the no-response watchdog blind to the seconds Twilio still spends
	// playing it.) playbackGen increments on every interruption so a reply
	// that is still being synthesized stops instead of talking over the caller.
	speakingMu    sync.Mutex
	speakingUntil time.Time
	playbackGen   int
	markSeq       int
	latestMark    string
	lastAgentText string
	lastSpeakEnd  time.Time

	// Turn-taking state, see turn.go.
	turnMu       sync.Mutex
	turnRunMu    sync.Mutex
	turnGen      int
	turnBusy     int
	pending      string
	pendingTimer *time.Timer
	speakMu      sync.Mutex

	// Guards the no-response retry count, the idle-activity clock, and
	// whether the call has already been finalized, so the watchdog
	// goroutine and the main Twilio-read/transcript goroutines never race
	// on them. See touchActivity/checkWatchdog: a single idle-time
	// watchdog (modeled on Pipecat/Dograh's UserIdleController) drives both
	// no-response escalation and max-call-duration enforcement, instead of
	// a one-shot timer that has to be correctly rearmed from every call
	// site — that rearm-everywhere approach previously had a real gap
	// (a caller who trails off mid-utterance, producing partial transcripts
	// but never a final one, would cancel the timer and never restart it).
	stateMu         sync.Mutex
	noResponseCount int
	lastActivity    time.Time
	ended           bool
	watchdogStop    chan struct{}
}

func New(conn *websocket.Conn, cfg Config) *Session {
	return &Session{
		cfg:           cfg,
		conn:          conn,
		backendClient: backend.NewClient(cfg.BackendBaseURL, cfg.InternalToken),
		watchdogStop:  make(chan struct{}),
	}
}

// readIdleTimeout bounds how long we'll wait for the next Twilio Media
// Streams frame. During a connected call Twilio sends "media" events
// continuously (every ~20ms), so this only ever fires once the call has
// actually ended — it's the fallback for when Twilio's "stop" event and/or
// the WebSocket close frame never arrive (observed in production: a call
// that Twilio itself reports as "completed" can leave this socket open with
// no further frames, which without a deadline hangs Run() forever and skips
// finalization entirely).
const readIdleTimeout = 30 * time.Second

// Run blocks until the Twilio connection closes, a stop event arrives, or
// the peer goes idle for readIdleTimeout.
func (s *Session) Run() {
	defer s.cleanup()

	for {
		_ = s.conn.SetReadDeadline(time.Now().Add(readIdleTimeout))
		_, raw, err := s.conn.ReadMessage()
		if err != nil {
			log.Printf("[session %s] twilio read error: %v", s.callID, err)
			return
		}

		var msg twilio.InboundMessage
		if err := json.Unmarshal(raw, &msg); err != nil {
			log.Printf("[session %s] decode twilio message: %v", s.callID, err)
			continue
		}

		switch msg.Event {
		case "start":
			s.handleStart(msg.Start)
		case "media":
			s.handleMedia(msg.Media)
		case "mark":
			if msg.Mark != nil {
				s.handleMark(msg.Mark.Name)
			}
		case "stop":
			return
		}
	}
}

// NewBrowserTest starts a session for a browser voice test (see browserTest).
func NewBrowserTest(conn *websocket.Conn, cfg Config, sessionID, sessionToken string) *Session {
	s := New(conn, cfg)
	s.browserTest = true
	s.testSessionID = sessionID
	s.backendClient = backend.NewTestClient(cfg.BackendBaseURL, cfg.InternalToken, sessionToken)
	return s
}

// sendTestEvent forwards test-only information to a browser test client.
func (s *Session) sendTestEvent(event string, data any) {
	if s.browserTest {
		s.writeJSON(map[string]any{"event": event, "data": data})
	}
}

func (s *Session) handleStart(start *twilio.StartPayload) {
	s.streamSID = start.StreamSID
	s.callID = start.CustomParameters["callId"]
	if s.browserTest {
		s.callID = s.testSessionID
	}
	s.startedAt = time.Now()

	sarvamAPIKey := s.cfg.SarvamAPIKey
	language := s.cfg.DefaultLanguage
	if language == "" {
		language = "en-IN"
	}

	callConfig, err := s.backendClient.GetCallConfig(s.callID)
	if err != nil && s.browserTest {
		// A browser test must have a valid session ticket: never fall back to defaults.
		log.Printf("[session %s] browser test rejected: %v", s.callID, err)
		s.sendTestEvent("cb_error", map[string]any{"message": "This test session is invalid or expired. Start the test again."})
		_ = s.conn.Close()
		return
	}
	if err != nil {
		log.Printf("[session %s] failed to fetch call config, using gateway defaults: %v", s.callID, err)
		callConfig = &backend.CallConfig{
			MaxCallDurationSeconds:   300,
			MaxDurationMessage:       "I'm sorry, but I need to end our call now. Thank you for calling.",
			MaxNoResponseRetries:     2,
			NoResponseTimeoutSeconds: 6,
			NoResponsePrompt:         "Sorry, I didn't catch that — could you repeat it?",
			NoResponseEndMessage:     "I'll end the call here. Feel free to call back.",
		}
	} else {
		if callConfig.SarvamAPIKey != "" {
			sarvamAPIKey = callConfig.SarvamAPIKey
		}
		if callConfig.Language != "" {
			language = callConfig.Language
		}
	}
	s.callConfig = callConfig
	s.language = language
	s.voice = callConfig.Voice
	s.tts = sarvam.NewTTSClient(sarvamAPIKey)

	// Sarvam's own VAD default (500ms of silence = utterance finished) cuts
	// callers off mid-sentence on a normal speaking pause. 900ms gives real
	// hesitation room while still feeling responsive on a phone call.
	stt, err := sarvam.DialSTT(sarvamAPIKey, sttLanguage(language), sttSilenceMs)
	if err != nil {
		log.Printf("[session %s] failed to connect to Sarvam STT: %v", s.callID, err)
		// Without STT the call can't progress; say so and hang up instead of
		// leaving the caller on a silent line until Twilio times out.
		s.endCall(
			"I'm sorry, we're having technical difficulties. Please call back in a few minutes.",
			"stt_unavailable",
		)
		return
	}
	s.stt = stt

	go s.readTranscripts()
	go s.runWatchdog()
	// Off the Twilio read loop: synthesizing the greeting must not stop us
	// reading (and forwarding) the caller's audio.
	go func() {
		s.speak(callConfig.Greeting)
		s.touchActivity()
	}()
	s.touchActivity()
}

func (s *Session) handleMedia(media *twilio.MediaPayload) {
	if s.stt == nil || media == nil {
		return
	}
	if err := s.stt.SendAudio(media.Payload); err != nil {
		log.Printf("[session %s] failed to forward audio to stt: %v", s.callID, err)
	}
}

func (s *Session) readTranscripts() {
	for {
		ev, err := s.stt.Next()
		if err != nil {
			log.Printf("[session %s] stt stream closed: %v", s.callID, err)
			return
		}
		switch ev.Kind {
		case sarvam.EventSpeechStart, sarvam.EventSpeechEnd:
			// Caller audio activity: keeps the no-response clock from firing
			// while they talk, without interrupting (line echo also trips VAD).
			s.touchActivity()
		case sarvam.EventPartial:
			if ev.Text == "" {
				continue
			}
			s.sendTestEvent("cb_transcript", map[string]any{"text": ev.Text, "final": false})
			s.touchActivity()
			s.onCallerSpeaking(ev.Text)
		case sarvam.EventFinal:
			if ev.Text == "" {
				continue
			}
			s.sendTestEvent("cb_transcript", map[string]any{"text": ev.Text, "final": true})
			s.touchActivity()
			s.resetNoResponseCount()
			s.onFinalTranscript(ev.Text)
		}
	}
}

// actOn runs AFTER the reply has already been spoken over the live media
// stream — for "transfer"/"hangup" this tells the backend to move the call
// now, guaranteeing the caller heard the message first rather than racing a
// server-side redirect against audio the gateway hasn't sent yet.
func (s *Session) actOn(action, reason string) {
	switch action {
	case "transfer", "hangup":
		s.waitForPlayback()
		if !s.markEnded() {
			return
		}
		if err := s.backendClient.Finalize(s.callID, action, reason); err != nil {
			log.Printf("[session %s] finalize(%s) failed: %v", s.callID, action, err)
		}
	default:
		s.touchActivity()
	}
}

// watchdogInterval is how often the idle watchdog checks in — coarse enough
// to be cheap, fine enough that no-response/max-duration enforcement never
// lags more than a second behind its configured threshold.
const watchdogInterval = 1 * time.Second

// touchActivity records that the caller (or the agent, mid-turn) is
// demonstrably active right now — resets the idle clock the watchdog reads.
// Called on: any transcript (partial or final), speaking the greeting, and
// after every completed turn.
func (s *Session) touchActivity() {
	s.stateMu.Lock()
	s.lastActivity = time.Now()
	s.stateMu.Unlock()
}

// runWatchdog is the single clock driving both no-response escalation and
// max-call-duration enforcement — modeled on Pipecat/Dograh's
// UserIdleController, which independently watches wall-clock idle time
// rather than relying on transcript events to rearm a one-shot timer. It
// keeps running for the life of the call regardless of how many turns
// happen or don't, so a call that goes silent (no more partials, no more
// finals — e.g. the caller trails off, or Sarvam's STT gets stuck) is still
// bounded, and so is total call length even if every turn otherwise
// succeeds.
func (s *Session) runWatchdog() {
	ticker := time.NewTicker(watchdogInterval)
	defer ticker.Stop()
	for {
		select {
		case <-s.watchdogStop:
			return
		case <-ticker.C:
			s.checkWatchdog()
		}
	}
}

func (s *Session) checkWatchdog() {
	s.stateMu.Lock()
	if s.ended || s.callConfig == nil {
		s.stateMu.Unlock()
		return
	}
	idleFor := time.Since(s.lastActivity)
	s.stateMu.Unlock()

	elapsed := time.Since(s.startedAt)
	if maxDuration := time.Duration(s.callConfig.MaxCallDurationSeconds) * time.Second; maxDuration > 0 && elapsed >= maxDuration {
		log.Printf("[session %s] max call duration exceeded — ending call", s.callID)
		s.endCall(s.callConfig.MaxDurationMessage, "max_duration_exceeded")
		return
	}

	// Don't count idle time while the agent itself is talking — that's not
	// the caller going quiet, it's just how a turn looks mid-reply.
	// Keep the idle clock fresh while the agent talks or is preparing a reply:
	// the caller's silence only starts counting once the agent has finished,
	// otherwise a long reply ends with "didn't catch that" fired instantly.
	if s.isSpeaking() || s.agentBusy() {
		s.touchActivity()
		return
	}

	noResponseTimeout := time.Duration(s.callConfig.NoResponseTimeoutSeconds) * time.Second
	if idleFor < noResponseTimeout {
		return
	}

	s.stateMu.Lock()
	s.noResponseCount++
	count, max := s.noResponseCount, s.callConfig.MaxNoResponseRetries
	s.stateMu.Unlock()

	if count > max {
		log.Printf("[session %s] no response, retries exhausted — ending call", s.callID)
		s.endCall(s.callConfig.NoResponseEndMessage, "no_response_exhausted")
		return
	}

	log.Printf("[session %s] no response (retry %d/%d)", s.callID, count, max)
	s.speak(s.callConfig.NoResponsePrompt)
	s.touchActivity()
}

func (s *Session) resetNoResponseCount() {
	s.stateMu.Lock()
	s.noResponseCount = 0
	s.stateMu.Unlock()
}

// endCall speaks a closing message and then tells the backend to actively
// move the still-live call — shared by both watchdog-driven end reasons
// (max duration, no-response exhaustion).
func (s *Session) endCall(message, reason string) {
	s.speak(message)
	s.waitForPlayback()
	if !s.markEnded() {
		return
	}
	if err := s.backendClient.Finalize(s.callID, "hangup", reason); err != nil {
		log.Printf("[session %s] finalize(hangup) failed: %v", s.callID, err)
	}
}

// markEnded returns true the first time it's called for this session, and
// false on every call after — the guard that keeps a natural Twilio "stop"
// event from double-processing a call we already finalized ourselves (or
// vice versa).
func (s *Session) markEnded() bool {
	s.stateMu.Lock()
	defer s.stateMu.Unlock()
	if s.ended {
		return false
	}
	s.ended = true
	return true
}

type ttsResult struct {
	audio string
	err   error
}

// speak synthesizes the reply in chunks (see splitReply) and plays them in
// order. All chunks are requested concurrently, so while the first one plays
// the rest are already being synthesized — time-to-first-audio is one short
// sentence's TTS latency instead of the whole reply's.
// speak returns when the first clip of the reply was queued (zero if none was).
func (s *Session) speak(text string) (firstAudio time.Time) {
	chunks := splitReply(text)
	if len(chunks) == 0 || s.tts == nil {
		return
	}

	s.speakMu.Lock()
	defer s.speakMu.Unlock()
	s.rememberAgentText(text)

	gen := s.currentPlaybackGen()
	results := make([]chan ttsResult, len(chunks))
	for i, chunk := range chunks {
		results[i] = make(chan ttsResult, 1)
		go func(chunk string, out chan<- ttsResult) {
			audio, err := s.tts.Synthesize(chunk, ttsLanguage(chunk, s.language), s.voice)
			out <- ttsResult{audio: audio, err: err}
		}(chunk, results[i])
	}

	for i, ch := range results {
		result := <-ch
		if s.currentPlaybackGen() != gen {
			return // caller barged in; drop the rest of this reply
		}
		if result.err != nil {
			log.Printf("[session %s] tts synthesis failed: %v", s.callID, result.err)
			continue
		}
		if firstAudio.IsZero() {
			firstAudio = time.Now()
		}
		s.sendAudio(result.audio, i == len(results)-1)
	}
	return firstAudio
}

// sendAudio queues one clip with Twilio. Only the final clip of a reply gets
// a mark: Twilio echoes it back once everything before it has finished
// playing, which is how handleMark knows the agent has really stopped talking.
func (s *Session) sendAudio(base64Mulaw string, final bool) {
	s.speakingMu.Lock()
	now := time.Now()
	base := s.speakingUntil
	if base.Before(now) {
		base = now
	}
	s.speakingUntil = base.Add(mulawDuration(base64Mulaw) + playbackMargin)
	s.lastSpeakEnd = s.speakingUntil
	var markName string
	if final {
		s.markSeq++
		markName = fmt.Sprintf("reply-%d", s.markSeq)
		s.latestMark = markName
	}
	s.speakingMu.Unlock()

	s.writeJSON(twilio.NewOutboundMedia(s.streamSID, base64Mulaw))
	if final {
		s.writeJSON(twilio.NewOutboundMark(s.streamSID, markName))
	}
}

// handleMark ends the "speaking" state when Twilio confirms the latest reply
// finished playing. Acks for older replies (already superseded) are ignored.
func (s *Session) handleMark(name string) {
	s.speakingMu.Lock()
	defer s.speakingMu.Unlock()
	if name == s.latestMark {
		s.speakingUntil = time.Time{}
	}
}

func (s *Session) interruptIfSpeaking() {
	s.speakingMu.Lock()
	if time.Now().After(s.speakingUntil) {
		s.speakingMu.Unlock()
		return
	}
	s.playbackGen++
	s.speakingUntil = time.Time{}
	s.lastSpeakEnd = time.Now()
	s.speakingMu.Unlock()
	s.writeJSON(twilio.NewOutboundClear(s.streamSID))
}

// maxPlaybackWait caps how long a closing message can hold up a hangup or
// transfer, in case Twilio never acks the mark.
const maxPlaybackWait = 15 * time.Second

// waitForPlayback blocks until the caller has actually heard everything sent
// so far (Twilio's mark ack, or the estimated end), so a goodbye or transfer
// message isn't cut off by the call being redirected underneath it.
func (s *Session) waitForPlayback() {
	deadline := time.Now().Add(maxPlaybackWait)
	for s.isSpeaking() && time.Now().Before(deadline) {
		time.Sleep(50 * time.Millisecond)
	}
}

func (s *Session) isSpeaking() bool {
	s.speakingMu.Lock()
	defer s.speakingMu.Unlock()
	return time.Now().Before(s.speakingUntil)
}

func (s *Session) currentPlaybackGen() int {
	s.speakingMu.Lock()
	defer s.speakingMu.Unlock()
	return s.playbackGen
}

func (s *Session) writeJSON(v any) {
	s.writeMu.Lock()
	defer s.writeMu.Unlock()
	if err := s.conn.WriteJSON(v); err != nil {
		log.Printf("[session %s] write to twilio failed: %v", s.callID, err)
	}
}

// cleanup runs whenever Run() exits, however it exits — an explicit Twilio
// "stop" event, a read error, or the readIdleTimeout above. If nothing has
// finalized this call yet (no transfer/hangup action, no no-response
// exhaustion), that means Twilio itself ended the call, so we tell the
// backend it completed. markEnded() makes this a no-op when some other path
// already finalized it.
func (s *Session) cleanup() {
	close(s.watchdogStop)
	if s.markEnded() && s.callID != "" {
		duration := int(time.Since(s.startedAt).Seconds())
		if err := s.backendClient.End(s.callID, "completed", duration); err != nil {
			log.Printf("[session %s] failed to notify backend of call end: %v", s.callID, err)
		}
	}
	if s.stt != nil {
		_ = s.stt.Close()
	}
	_ = s.conn.Close()
}

// sttLanguage picks the language hint for speech recognition. Callers in India
// mix languages mid-sentence and often speak one other than the agent's voice
// language; pinning recognition to any single language (en-IN turned Hindi into
// nonsense English) breaks that. Sarvam's detection ("auto") handles every
// supported language per utterance, so the agent's language only drives TTS.
func sttLanguage(string) string { return "auto" }
