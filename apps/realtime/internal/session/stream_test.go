package session

import (
	"errors"
	"fmt"
	"sync"
	"testing"
	"time"
)

func TestSentenceMergerHoldsTinyFragments(t *testing.T) {
	var m sentenceMerger
	if _, ok := m.add("Ok."); ok {
		t.Fatal("a 3-character fragment must be held")
	}
	chunk, ok := m.add("I can book that for you.")
	if !ok || chunk != "Ok. I can book that for you." {
		t.Fatalf("got %q, %v", chunk, ok)
	}
	if chunk, ok := m.add("Is Tuesday at three fine for you?"); !ok || chunk != "Is Tuesday at three fine for you?" {
		t.Fatalf("a long sentence passes straight through, got %q", chunk)
	}
	m.add("Yes.")
	if chunk, ok := m.flush(); !ok || chunk != "Yes." {
		t.Fatalf("flush should release the held fragment, got %q", chunk)
	}
	if _, ok := m.flush(); ok {
		t.Fatal("nothing left to flush")
	}
}

func feed(items ...string) <-chan string {
	ch := make(chan string, len(items))
	for _, item := range items {
		ch <- item
	}
	close(ch)
	return ch
}

func never() bool { return false }

// one adapts a simple synthesizer to the clipSource shape: a single piece of audio.
func one(synth func(chunk string) (string, error)) clipSource {
	return func(chunk string) <-chan ttsResult {
		out := make(chan ttsResult, 1)
		go func() {
			defer close(out)
			audio, err := synth(chunk)
			out <- ttsResult{audio: audio, err: err}
		}()
		return out
	}
}

func TestPipelinePlaysInOrderEvenWhenLaterClipsFinishFirst(t *testing.T) {
	delays := map[string]time.Duration{
		"First sentence goes here.":  80 * time.Millisecond,
		"Second sentence goes here.": 5 * time.Millisecond,
		"Third sentence goes here.":  5 * time.Millisecond,
	}
	var mu sync.Mutex
	var played []string
	first, n := pipeline(
		feed("First sentence goes here.", "Second sentence goes here.", "Third sentence goes here."),
		one(func(chunk string) (string, error) { time.Sleep(delays[chunk]); return "audio:" + chunk, nil }),
		func(audio string) { mu.Lock(); played = append(played, audio); mu.Unlock() },
		never,
		func(string) {},
	)
	want := []string{"audio:First sentence goes here.", "audio:Second sentence goes here.", "audio:Third sentence goes here."}
	if fmt.Sprint(played) != fmt.Sprint(want) || n != 3 || first.IsZero() {
		t.Fatalf("played %v (n=%d), want %v", played, n, want)
	}
}

func TestPipelineStartsSynthesisBeforeTheReplyIsComplete(t *testing.T) {
	ch := make(chan string)
	started := make(chan string, 4)
	done := make(chan struct{})
	go func() {
		pipeline(ch,
			one(func(chunk string) (string, error) { started <- chunk; return "a", nil }),
			func(string) {}, never, func(string) {})
		close(done)
	}()
	ch <- "The first sentence arrives early."
	select {
	case got := <-started:
		if got != "The first sentence arrives early." {
			t.Fatalf("unexpected chunk %q", got)
		}
	case <-time.After(time.Second):
		t.Fatal("TTS must start while the stream is still open")
	}
	close(ch)
	<-done
}

func TestPipelineSkipsFailedClipsAndKeepsGoing(t *testing.T) {
	var played []string
	_, n := pipeline(
		feed("This one fails to synthesize.", "This one works fine though."),
		one(func(chunk string) (string, error) {
			if chunk == "This one fails to synthesize." {
				return "", errors.New("tts down")
			}
			return "ok", nil
		}),
		func(audio string) { played = append(played, audio) },
		never, func(string) {},
	)
	if n != 1 || len(played) != 1 {
		t.Fatalf("played %v (n=%d)", played, n)
	}
}

func TestPipelineStopsPlayingAfterABargeIn(t *testing.T) {
	var mu sync.Mutex
	interrupted := false
	var played int
	pipeline(
		feed("First sentence is long enough.", "Second sentence is long enough.", "Third sentence is long enough."),
		one(func(chunk string) (string, error) { return "a", nil }),
		func(string) {
			mu.Lock()
			played++
			interrupted = true // the caller speaks right after the first clip
			mu.Unlock()
		},
		func() bool { mu.Lock(); defer mu.Unlock(); return interrupted },
		func(string) {},
	)
	if played != 1 {
		t.Fatalf("expected exactly the first clip to play, got %d", played)
	}
}

func TestPipelineReportsEveryQueuedChunk(t *testing.T) {
	var chunks []string
	pipeline(feed("Ok.", "Please hold on a moment."),
		one(func(string) (string, error) { return "a", nil }), func(string) {}, never,
		func(c string) { chunks = append(chunks, c) })
	if len(chunks) != 1 || chunks[0] != "Ok. Please hold on a moment." {
		t.Fatalf("chunks %v", chunks)
	}
}

func TestPipelinePlaysEveryPieceOfAStreamedClipAsItArrives(t *testing.T) {
	release := make(chan struct{})
	synth := func(chunk string) <-chan ttsResult {
		out := make(chan ttsResult, 4)
		go func() {
			defer close(out)
			out <- ttsResult{audio: "piece-1"}
			<-release // the rest of the sentence is still being synthesized
			out <- ttsResult{audio: "piece-2"}
		}()
		return out
	}
	played := make(chan string, 4)
	done := make(chan struct{})
	go func() {
		pipeline(feed("A sentence that streams in."), synth, func(a string) { played <- a }, never, func(string) {})
		close(done)
	}()
	select {
	case got := <-played:
		if got != "piece-1" {
			t.Fatalf("got %q", got)
		}
	case <-time.After(time.Second):
		t.Fatal("the first piece must play before the sentence finishes synthesizing")
	}
	close(release)
	<-done
	if got := <-played; got != "piece-2" {
		t.Fatalf("got %q", got)
	}
}
