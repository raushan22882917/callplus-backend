package sarvam

import (
	"bytes"
	"context"
	"encoding/base64"
	"fmt"
	"io"
	"mime/multipart"
	"net/http"
	"time"
)

// CloneVoicePrefix starts every Sarvam cloned voice id. An agent whose voice id starts with it speaks through the
// cloning endpoints instead of Bulbul's named voices.
const CloneVoicePrefix = "svc-"

// cloneBaseURL is a variable so tests can point it at a local server.
var cloneBaseURL = "https://api.sarvam.ai"

const (
	cloneRate   = 8000
	clonePiece  = 6400 // bytes of 16-bit audio per piece handed on: 200 ms
	cloneTimout = 30 * time.Second
)

// CloneClient speaks text in a Sarvam cloned voice. It asks for raw 16-bit audio at 8 kHz (documented as headerless
// PCM) and converts it to the mu-law a phone line plays, so nothing depends on how the service frames mu-law.
type CloneClient struct {
	apiKey string
	http   *http.Client
}

func NewCloneClient(apiKey string) *CloneClient {
	return &CloneClient{apiKey: apiKey, http: &http.Client{}}
}

// IsCloneVoice reports whether a voice id is a Sarvam cloned voice.
func IsCloneVoice(voice string) bool {
	return len(voice) > len(CloneVoicePrefix) && voice[:len(CloneVoicePrefix)] == CloneVoicePrefix
}

// Stream speaks text and calls onAudio with base64 mu-law audio as it arrives. delivered is the number of pieces
// handed on, so the caller knows whether anything was already spoken when an error comes back. languageCode is
// optional: without it Sarvam detects the language.
func (c *CloneClient) Stream(text, voiceID, languageCode string, onAudio func(base64Mulaw string)) (delivered int, err error) {
	var body bytes.Buffer
	w := multipart.NewWriter(&body)
	fields := [][2]string{
		{"text", text},
		{"voice_id", voiceID},
		{"output_audio_codec", "linear16"},
		{"speech_sample_rate", fmt.Sprint(cloneRate)},
	}
	if languageCode != "" {
		fields = append(fields, [2]string{"language_code", languageCode})
	}
	for _, f := range fields {
		if err := w.WriteField(f[0], f[1]); err != nil {
			return 0, fmt.Errorf("build clone request: %w", err)
		}
	}
	if err := w.Close(); err != nil {
		return 0, fmt.Errorf("build clone request: %w", err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), cloneTimout)
	defer cancel()
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, cloneBaseURL+"/voices/clone/stream", &body)
	if err != nil {
		return 0, fmt.Errorf("build clone request: %w", err)
	}
	req.Header.Set("Content-Type", w.FormDataContentType())
	req.Header.Set("api-subscription-key", c.apiKey)

	resp, err := c.http.Do(req)
	if err != nil {
		return 0, fmt.Errorf("call sarvam clone tts: %w", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode >= 400 {
		detail, _ := io.ReadAll(io.LimitReader(resp.Body, 400))
		return 0, fmt.Errorf("sarvam clone tts failed (%d): %s", resp.StatusCode, string(detail))
	}

	buf := make([]byte, clonePiece)
	for {
		n, readErr := io.ReadFull(resp.Body, buf)
		n -= n % 2 // whole 16-bit samples only; a trailing odd byte cannot be played
		if n > 0 {
			onAudio(base64.StdEncoding.EncodeToString(pcm16ToMulaw(buf[:n])))
			delivered++
		}
		if readErr == io.EOF || readErr == io.ErrUnexpectedEOF {
			return delivered, nil
		}
		if readErr != nil {
			return delivered, fmt.Errorf("read sarvam clone audio: %w", readErr)
		}
	}
}

// pcm16ToMulaw converts little-endian 16-bit samples to G.711 mu-law, one byte per sample.
func pcm16ToMulaw(pcm []byte) []byte {
	out := make([]byte, len(pcm)/2)
	for i := range out {
		out[i] = linearToMulaw(int16(uint16(pcm[2*i]) | uint16(pcm[2*i+1])<<8))
	}
	return out
}

func linearToMulaw(sample int16) byte {
	const bias = 0x84
	const clip = 32635
	s := int(sample)
	sign := 0
	if s < 0 {
		sign = 0x80
		s = -s
	}
	if s > clip {
		s = clip
	}
	s += bias
	exponent := 7
	for mask := 0x4000; s&mask == 0 && exponent > 0; mask >>= 1 {
		exponent--
	}
	mantissa := (s >> (exponent + 3)) & 0x0f
	return ^byte(sign | exponent<<4 | mantissa)
}
