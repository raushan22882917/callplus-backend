// Package elevenlabs is the text-to-speech provider for calls outside India: a workspace that connects its own
// ElevenLabs key lets an agent speak in any of its voices, in dozens of languages.
//
// ElevenLabs can return 8 kHz mu-law directly ("ulaw_8000"), which is exactly what a phone line plays, so the
// audio goes to the caller with no conversion. It is streamed: the first part of a sentence plays while the
// rest is still being made.
package elevenlabs

import (
	"bytes"
	"context"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"time"
)

// baseURL is a variable so tests can point it at a local server.
var baseURL = "https://api.elevenlabs.io"

// DefaultModel is ElevenLabs' fastest multilingual model, which is what a live call needs.
const DefaultModel = "eleven_flash_v2_5"

// pieceBytes is how much audio is handed on at a time: 400 ms at 8 kHz.
const pieceBytes = 3200

type Client struct {
	apiKey string
	model  string
	http   *http.Client
}

func NewClient(apiKey, model string) *Client {
	if model == "" {
		model = DefaultModel
	}
	return &Client{apiKey: apiKey, model: model, http: &http.Client{}}
}

type request struct {
	Text          string        `json:"text"`
	ModelID       string        `json:"model_id"`
	VoiceSettings voiceSettings `json:"voice_settings"`
}

type voiceSettings struct {
	Stability       float64 `json:"stability"`
	SimilarityBoost float64 `json:"similarity_boost"`
}

// Stream speaks text in the given voice and calls onAudio with base64 mu-law audio as it arrives. delivered is
// the number of pieces handed on, so the caller knows whether anything was already spoken when an error comes
// back.
func (c *Client) Stream(text, voiceID string, onAudio func(base64Mulaw string)) (delivered int, err error) {
	if voiceID == "" {
		return 0, fmt.Errorf("elevenlabs: this agent has no voice chosen")
	}
	body, err := json.Marshal(request{
		Text:          text,
		ModelID:       c.model,
		VoiceSettings: voiceSettings{Stability: 0.5, SimilarityBoost: 0.8},
	})
	if err != nil {
		return 0, fmt.Errorf("marshal elevenlabs request: %w", err)
	}
	endpoint := fmt.Sprintf("%s/v1/text-to-speech/%s/stream?output_format=ulaw_8000", baseURL, url.PathEscape(voiceID))
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, endpoint, bytes.NewReader(body))
	if err != nil {
		return 0, fmt.Errorf("build elevenlabs request: %w", err)
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("xi-api-key", c.apiKey)

	resp, err := c.http.Do(req)
	if err != nil {
		return 0, fmt.Errorf("call elevenlabs: %w", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode >= 400 {
		detail, _ := io.ReadAll(io.LimitReader(resp.Body, 400))
		return 0, fmt.Errorf("elevenlabs tts failed (%d): %s", resp.StatusCode, string(detail))
	}

	buf := make([]byte, pieceBytes)
	for {
		n, readErr := io.ReadFull(resp.Body, buf)
		if n > 0 {
			onAudio(base64.StdEncoding.EncodeToString(buf[:n]))
			delivered++
		}
		if readErr == io.EOF || readErr == io.ErrUnexpectedEOF {
			return delivered, nil
		}
		if readErr != nil {
			return delivered, fmt.Errorf("read elevenlabs audio: %w", readErr)
		}
	}
}
