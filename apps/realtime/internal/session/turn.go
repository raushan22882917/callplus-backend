package session

import (
	"log"
	"strings"
	"sync"
	"sync/atomic"
	"time"
	"unicode"

	"callcenter/realtime/internal/backend"
)

// Turn-taking. The original loop did everything inline on the STT read
// goroutine: a final transcript called the backend (LLM, seconds), then
// synthesized speech, all while nothing was reading STT. Anything the caller
// said in that window queued up and was answered one stale utterance at a
// time, long after the fact, and could not cancel a reply in flight. That is
// what made the agent seem to talk non-stop and ignore the caller.
//
// Now: final transcripts are merged for turnDebounce (a caller who pauses
// mid-thought isn't answered twice), turns run one at a time on their own
// goroutine, and any real speech from the caller bumps turnGen so a reply
// that is still being generated is dropped instead of spoken over them.

const (
	// turnDebounce merges back-to-back final transcripts into one turn. Sarvam's
	// VAD already waits sttSilenceMs of silence before a final transcript, so this
	// only needs to catch a second final arriving right behind the first.
	turnDebounce = 150 * time.Millisecond
	// fillerDelay: if the reply isn't ready by then (a booking or lookup is
	// running), say a short "one moment" so the caller isn't left in silence.
	fillerDelay = 1200 * time.Millisecond
	// minBargeInWords: partial words needed to interrupt the agent
	// mid-reply. Two filters a cough or "um" yet reacts to "wait, stop".
	minBargeInWords = 2
	// echoWindow: after the agent stops talking, transcripts that merely
	// repeat what it said are treated as line echo, not the caller.
	echoWindow = 3 * time.Second
	// echoOverlap: fraction of heard words found in the agent's last reply
	// for the transcript to count as echo.
	echoOverlap = 0.7
	// echoMinWords: never discard very short utterances as echo ("yes",
	// "okay" are often genuinely repeated).
	echoMinWords = 4
)

func (s *Session) onFinalTranscript(text, language string) {
	if s.isEcho(text) {
		log.Printf("[session %s] ignoring echo of agent speech: %q", s.callID, text)
		return
	}
	s.interruptIfSpeaking()

	s.turnMu.Lock()
	s.turnGen++
	gen := s.turnGen
	if s.pending != "" {
		s.pending += " "
	}
	s.pending += text
	if language != "" {
		s.heardLanguage = language
	}
	if s.pendingTimer != nil {
		s.pendingTimer.Stop()
	}
	s.pendingTimer = time.AfterFunc(turnDebounce, func() { s.runTurn(gen) })
	s.turnMu.Unlock()
}

// onCallerSpeaking is called for partial transcripts. Real speech cancels a
// reply that is still being generated and, past a few words, cuts off the
// agent's playback.
func (s *Session) onCallerSpeaking(text string) {
	words := len(strings.Fields(text))
	if words == 0 || s.isEcho(text) {
		return
	}
	if words >= minBargeInWords {
		s.interruptIfSpeaking()
		s.turnMu.Lock()
		if s.turnBusy > 0 {
			s.turnGen++ // the in-flight reply is now stale
		}
		s.turnMu.Unlock()
	}
}

func (s *Session) runTurn(gen int) {
	s.turnRunMu.Lock() // one turn at a time: backend history stays ordered
	defer s.turnRunMu.Unlock()

	s.turnMu.Lock()
	if gen != s.turnGen || s.pending == "" {
		s.turnMu.Unlock()
		return
	}
	text := s.pending
	language := s.heardLanguage
	s.pending = ""
	s.heardLanguage = ""
	s.turnBusy++
	s.turnMu.Unlock()
	defer func() {
		s.turnMu.Lock()
		s.turnBusy--
		s.turnMu.Unlock()
		s.touchActivity()
	}()

	log.Printf("[session %s] heard: %q", s.callID, text)
	started := time.Now()
	stopFiller := make(chan struct{})
	fillerDone := make(chan struct{})
	var stopOnce sync.Once
	endFiller := func() {
		stopOnce.Do(func() { close(stopFiller) })
		<-fillerDone // a filler that already started finishes before the reply
	}
	go func() {
		defer close(fillerDone)
		select {
		case <-time.After(fillerDelay):
			if !s.turnIsStale(gen) && !s.isEnded() {
				s.sendTestEvent("cb_filler", map[string]any{"after_ms": fillerDelay.Milliseconds()})
				s.speak(fillerPhrase(ttsLanguage(text, s.language)))
			}
		case <-stopFiller:
		}
	}()

	var (
		result     *backend.TurnResponse
		err        error
		firstAudio time.Time
		streamed   int
		replyMs    int64
	)
	if s.cfg.StreamReplies && s.tts != nil && s.backendClient.CanStream() {
		// A hung-up caller cancels synthesis too: there is nobody left to hear it.
		result, streamed, firstAudio, err = s.streamingTurn(
			text, language,
			func() bool { return s.turnIsStale(gen) || s.isEnded() },
			endFiller,
		)
		if err != nil && streamed == 0 {
			log.Printf("[session %s] streaming turn failed before any speech, retrying normally: %v", s.callID, err)
			result, err = s.backendClient.Turn(s.callID, text, language)
		}
	} else {
		result, err = s.backendClient.Turn(s.callID, text, language)
	}
	replyMs = time.Since(started).Milliseconds()
	endFiller()

	if s.turnIsStale(gen) {
		log.Printf("[session %s] caller spoke again; dropping stale reply", s.callID)
		if err == nil && result != nil {
			s.sendTestEvent("cb_turn_dropped", map[string]any{"reply_message_id": result.ReplyMessageID})
			if rerr := s.backendClient.RetractReply(s.callID, result.ReplyMessageID); rerr != nil {
				log.Printf("[session %s] retract unspoken reply failed: %v", s.callID, rerr)
			}
		}
		return
	}
	if s.isEnded() {
		// The caller hung up while the reply was being prepared. The transcript keeps it; nothing is
		// spoken, so no speech is synthesized for a closed line.
		log.Printf("[session %s] call ended while replying; not speaking", s.callID)
		return
	}
	if err != nil {
		log.Printf("[session %s] backend turn call failed: %v", s.callID, err)
		if streamed == 0 {
			s.speak("I'm sorry, I'm having trouble right now. Could you say that again?")
		}
		return
	}
	log.Printf("[session %s] replying: %q (action=%s, streamed=%d)", s.callID, result.ReplyText, result.Action, streamed)
	s.sendTestEvent("cb_turn", map[string]any{"heard": text, "result": result.Raw})
	if streamed == 0 {
		firstAudio = s.speak(result.ReplyText)
	} else {
		s.rememberAgentText(result.ReplyText)
	}
	if !firstAudio.IsZero() {
		// Test-only timeline: from the end of the caller's turn to the reply text, then to its first audio.
		s.sendTestEvent("cb_timing", map[string]any{
			"wait_ms":        turnDebounce.Milliseconds(),
			"reply_ms":       replyMs,
			"first_audio_ms": firstAudio.Sub(started).Milliseconds(),
			"streamed":       streamed,
		})
	}
	s.actOn(result.Action, result.Reason)
}

func (s *Session) turnIsStale(gen int) bool {
	s.turnMu.Lock()
	defer s.turnMu.Unlock()
	return gen != s.turnGen
}

// agentBusy is true while a turn is pending or in flight; the no-response
// watchdog must not fire then, or it talks over the reply being prepared.
func (s *Session) agentBusy() bool {
	s.turnMu.Lock()
	defer s.turnMu.Unlock()
	return s.turnBusy > 0 || s.pending != ""
}

func (s *Session) rememberAgentText(text string) {
	s.speakingMu.Lock()
	s.lastAgentText = text
	s.speakingMu.Unlock()
}

// isEcho reports whether text is (mostly) the agent's own last reply coming
// back through the caller's line — speakerphone or handset leakage — while
// the agent is speaking or just finished.
func (s *Session) isEcho(text string) bool {
	s.speakingMu.Lock()
	last := s.lastAgentText
	recent := time.Now().Before(s.speakingUntil) || time.Since(s.lastSpeakEnd) < echoWindow
	s.speakingMu.Unlock()
	if !recent || last == "" {
		return false
	}
	return looksLikeEcho(text, last)
}

func looksLikeEcho(heard, spoken string) bool {
	heardWords := normalizeWords(heard)
	if len(heardWords) < echoMinWords {
		return false
	}
	spokenSet := map[string]struct{}{}
	for _, w := range normalizeWords(spoken) {
		spokenSet[w] = struct{}{}
	}
	hits := 0
	for _, w := range heardWords {
		if _, ok := spokenSet[w]; ok {
			hits++
		}
	}
	return float64(hits)/float64(len(heardWords)) >= echoOverlap
}

func normalizeWords(s string) []string {
	return strings.FieldsFunc(strings.ToLower(s), func(r rune) bool {
		return !unicode.IsLetter(r) && !unicode.IsNumber(r)
	})
}

// fillerPhrases: what the agent says while a slow reply (a booking, a lookup) is
// still being prepared. Short, so it never talks over the real answer for long,
// and several per language, handed out in turn: hearing the identical "One
// moment, please." every time is one of the first things that gives a bot away.
// None of them marks the speaker's gender.
var fillerPhrases = map[string][]string{
	"en-IN": {"One sec.", "Let me check that.", "Sure, one moment.", "Hmm, let me see."},
	"hi-IN": {"एक सेकंड।", "हाँ जी, एक मिनट।", "ठीक है, बस एक सेकंड।", "अच्छा, एक मिनट।"},
	"mr-IN": {"एक क्षण थांबा.", "ठीक आहे, एक सेकंद."},
	"bn-IN": {"এক মুহূর্ত।", "আচ্ছা, এক সেকেন্ড।"},
	"gu-IN": {"એક ક્ષણ.", "સારું, એક સેકન્ડ."},
	"pa-IN": {"ਇੱਕ ਪਲ।", "ਠੀਕ ਹੈ, ਇੱਕ ਸਕਿੰਟ।"},
	"od-IN": {"ଗୋଟିଏ ମୁହୂର୍ତ୍ତ।", "ଠିକ୍ ଅଛି, ଏକ ସେକେଣ୍ଡ।"},
	"ta-IN": {"ஒரு நிமிடம்.", "சரி, ஒரு நொடி."},
	"te-IN": {"ఒక్క క్షణం.", "సరే, ఒక్క సెకను."},
	"kn-IN": {"ಒಂದು ಕ್ಷಣ.", "ಸರಿ, ಒಂದು ಸೆಕೆಂಡ್."},
	"ml-IN": {"ഒരു നിമിഷം.", "ശരി, ഒരു സെക്കൻഡ്."},
}

var fillerCounter atomic.Uint32

func fillerPhrase(language string) string {
	phrases, ok := fillerPhrases[language]
	if !ok {
		phrases = fillerPhrases["en-IN"]
	}
	return phrases[int(fillerCounter.Add(1)-1)%len(phrases)]
}
