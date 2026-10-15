package sarvam

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"time"
)

const ttsRESTURL = "https://api.sarvam.ai/text-to-speech"

// TTSClient uses the REST endpoint, not the streaming one: the turn
// orchestrator (see internal/session) already gets the agent's reply as one
// complete string from the backend, so there's no partial text to stream
// into TTS incrementally — REST is simpler with no latency cost here.
type TTSClient struct {
	apiKey     string
	httpClient *http.Client
}

func NewTTSClient(apiKey string) *TTSClient {
	return &TTSClient{apiKey: apiKey, httpClient: &http.Client{Timeout: 20 * time.Second}}
}

type ttsRequest struct {
	Text             string `json:"text"`
	LanguageCode     string `json:"language_code"`
	Speaker          string `json:"speaker,omitempty"`
	Model            string `json:"model"`
	SpeechSampleRate int    `json:"speech_sample_rate"`
	OutputAudioCodec string `json:"output_audio_codec"`
}

type ttsResponse struct {
	Audios []string `json:"audios"`
}

// Synthesize returns base64-encoded mulaw/8kHz audio — playable directly
// back to Twilio with zero transcoding. speaker is a bulbul:v3 voice name
// (e.g. "priya", "shubh") — see the AIAgent's configured voice; an empty
// string lets Sarvam use its own default.
func (c *TTSClient) Synthesize(text, languageCode, speaker string) (string, error) {
	body, err := json.Marshal(ttsRequest{
		Text:             text,
		LanguageCode:     languageCode,
		Speaker:          speaker,
		Model:            "bulbul:v3", // v2 is deprecated — confirmed via a live API call
		SpeechSampleRate: 8000,
		OutputAudioCodec: "mulaw",
	})
	if err != nil {
		return "", fmt.Errorf("marshal tts request: %w", err)
	}

	req, err := http.NewRequest(http.MethodPost, ttsRESTURL, bytes.NewReader(body))
	if err != nil {
		return "", fmt.Errorf("build tts request: %w", err)
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("api-subscription-key", c.apiKey)

	resp, err := c.httpClient.Do(req)
	if err != nil {
		return "", fmt.Errorf("call sarvam tts: %w", err)
	}
	defer resp.Body.Close()

	respBody, err := io.ReadAll(resp.Body)
	if err != nil {
		return "", fmt.Errorf("read tts response: %w", err)
	}
	if resp.StatusCode >= 400 {
		return "", fmt.Errorf("sarvam tts failed (%d): %s", resp.StatusCode, string(respBody))
	}

	var parsed ttsResponse
	if err := json.Unmarshal(respBody, &parsed); err != nil {
		return "", fmt.Errorf("decode tts response: %w", err)
	}
	if len(parsed.Audios) == 0 {
		return "", fmt.Errorf("sarvam tts returned no audio")
	}
	return parsed.Audios[0], nil
}
