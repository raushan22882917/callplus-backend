package session

import (
	"log"
	"sync"
	"time"
	"unicode/utf8"

	"callcenter/realtime/internal/backend"
)

// Streaming replies. The backend sends the agent's reply sentence by sentence
// while the LLM is still writing it. Each sentence goes to TTS the moment it
// arrives and plays as soon as the clips before it have, so the caller hears the
// first words after "first sentence written + one short TTS request" instead of
// "whole reply written + first sentence synthesized".

// sentenceMerger glues fragments shorter than firstChunkMinChars onto the next
// sentence ("Ok." on its own makes speech choppy for no latency gain).
type sentenceMerger struct{ held string }

func (m *sentenceMerger) add(sentence string) (chunk string, ok bool) {
	if m.held != "" {
		sentence = m.held + " " + sentence
		m.held = ""
	}
	if utf8.RuneCountInString(sentence) < firstChunkMinChars {
		m.held = sentence
		return "", false
	}
	return sentence, true
}

func (m *sentenceMerger) flush() (chunk string, ok bool) {
	chunk, m.held = m.held, ""
	return chunk, chunk != ""
}

// clipSource synthesizes one chunk of text. Audio arrives on the returned
// channel in order, as one or many pieces, and the channel is closed when the
// chunk is finished; a piece with err set ends it early. The WebSocket TTS sends
// many small pieces (playback starts on the first); REST sends exactly one.
type clipSource func(chunk string) <-chan ttsResult

// pipeline runs the order-preserving speak loop: every chunk is synthesized
// concurrently the instant it is received, audio plays strictly in order, and a
// failed or cancelled chunk never blocks the ones after it. It returns when
// `sentences` is closed and everything has played (or been dropped).
//
//	synth      turns one chunk into audio
//	play       hands one piece of audio to the caller's phone line
//	cancelled  reports a barge-in or a stale turn: stop synthesizing, drop the rest
//	onChunk    is told about each chunk as it is queued (echo detection)
//
// It reports when the first audio was played (zero if none) and how many pieces.
func pipeline(
	sentences <-chan string,
	synth clipSource,
	play func(audio string),
	cancelled func() bool,
	onChunk func(chunk string),
) (firstAudio time.Time, played int) {
	queue := make(chan (<-chan ttsResult), 64)
	var player sync.WaitGroup
	player.Add(1)
	go func() {
		defer player.Done()
		for pending := range queue {
			for result := range pending {
				if cancelled() {
					continue // keep draining so synthesizers finish, play nothing more
				}
				if result.err != nil {
					log.Printf("tts synthesis failed: %v", result.err)
					continue
				}
				if firstAudio.IsZero() {
					firstAudio = time.Now()
				}
				played++
				play(result.audio)
			}
		}
	}()

	enqueue := func(chunk string) {
		if cancelled() {
			return
		}
		onChunk(chunk)
		queue <- synth(chunk)
	}

	var merger sentenceMerger
	for sentence := range sentences {
		if chunk, ok := merger.add(sentence); ok {
			enqueue(chunk)
		}
	}
	if chunk, ok := merger.flush(); ok {
		enqueue(chunk)
	}
	close(queue)
	player.Wait()
	return firstAudio, played
}

// speakStream plays sentences as they arrive on `sentences` (the caller closes
// it when the reply is complete) and returns when the first clip was queued.
// stale reports a turn the caller has already talked over.
func (s *Session) speakStream(sentences <-chan string, stale func() bool) time.Time {
	s.speakMu.Lock()
	defer s.speakMu.Unlock()
	s.rememberAgentText("")

	gen := s.currentPlaybackGen()
	interrupted := func() bool { return s.currentPlaybackGen() != gen || stale() }
	first, played := pipeline(
		sentences,
		s.synthesize,
		func(audio string) { s.sendAudio(audio, false) },
		interrupted,
		s.appendAgentText,
	)
	// One mark after the last clip: Twilio echoes it once everything before it
	// has played, which is how the session learns the agent stopped talking.
	if played > 0 && !interrupted() {
		s.sendMark()
	}
	return first
}

// streamingTurn runs one turn over the streaming endpoint, speaking each sentence
// as it arrives. streamed is how many sentences were spoken; with streamed == 0
// and an error nothing reached the caller and the ordinary Turn call is safe.
func (s *Session) streamingTurn(
	text, callerLanguage string, stale func() bool, stopFiller func(),
) (resp *backend.TurnResponse, streamed int, firstAudio time.Time, err error) {
	sentences := make(chan string, 32)
	var once sync.Once
	spoke := make(chan time.Time, 1)
	onSentence := func(sentence string) {
		once.Do(func() {
			stopFiller() // the first words are here; no "one moment" needed
			go func() { spoke <- s.speakStream(sentences, stale) }()
		})
		sentences <- sentence
	}

	resp, streamed, err = s.backendClient.TurnStream(s.callID, text, callerLanguage, onSentence)
	if streamed > 0 {
		close(sentences)
		firstAudio = <-spoke
	}
	return resp, streamed, firstAudio, err
}
