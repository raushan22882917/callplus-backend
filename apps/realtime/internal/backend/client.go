// Package backend calls the FastAPI service's internal/* endpoints — the
// "AI Engine" (LLM reasoning + tool execution) lives there, not here. This
// gateway's whole job is telephony plumbing: get audio in, get a transcript,
// hand it to the backend, get reply text back, turn it into audio, send it
// out. See apps/api/app/api/v1/internal.py for the other side.
package backend

import (
	"bufio"
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"strings"
	"time"
)

type Client struct {
	baseURL       string // e.g. http://api:8000/api/v1/internal
	internalToken string
	httpClient    *http.Client
	// "calls" for phone calls; "test-calls" for Flow Studio browser voice tests,
	// which the backend runs on its test engine (simulated tools, nothing saved).
	callsPath   string
	configQuery string
}

func NewClient(baseURL, internalToken string) *Client {
	return &Client{
		baseURL:       baseURL,
		internalToken: internalToken,
		httpClient:    &http.Client{Timeout: 30 * time.Second},
		callsPath:     "calls",
	}
}

// NewTestClient is NewClient for a browser test session: same calls, routed to
// /internal/test-calls/*, with the browser's session ticket checked at config time.
func NewTestClient(baseURL, internalToken, sessionToken string) *Client {
	c := NewClient(baseURL, internalToken)
	c.callsPath = "test-calls"
	c.configQuery = "?token=" + url.QueryEscape(sessionToken)
	return c
}

// TurnResponse.Action tells the gateway what to do AFTER it finishes
// speaking ReplyText: "none" (nothing further), or "transfer"/"hangup" —
// in both of those cases, call Finalize with the same action once playback
// is done, so the caller always hears the message before the call moves.
type TurnResponse struct {
	ReplyText string `json:"reply_text"`
	Action    string `json:"action"`
	Reason    string `json:"reason"`
	// ReplyMessageID identifies the saved AI message so a reply that is
	// dropped unspoken can be retracted (see RetractReply).
	ReplyMessageID string `json:"reply_message_id"`
	// Raw is the full response body; browser test sessions forward it (it
	// carries the test timeline: node, tool calls, latency, tokens).
	Raw json.RawMessage `json:"-"`
}

// CallConfig is resolved per-tenant by the backend — a tenant's own Sarvam
// key if they've connected one (see app/integrations/credentials.py),
// otherwise the platform's, plus this call's owner-configured call-control
// policy (from the assigned AI agent). This is what lets more than one
// tenant actually use the platform: without it, every call would use one
// shared key/policy regardless of which tenant it's for.
type CallConfig struct {
	SarvamAPIKey             string `json:"sarvam_api_key"`
	Language                 string `json:"language"`
	Voice                    string `json:"voice"`
	Greeting                 string `json:"greeting"`
	MaxCallDurationSeconds   int    `json:"max_call_duration_seconds"`
	MaxDurationMessage       string `json:"max_duration_message"`
	NoResponseTimeoutSeconds int    `json:"no_response_timeout_seconds"`
	MaxNoResponseRetries     int    `json:"max_no_response_retries"`
	NoResponsePrompt         string `json:"no_response_prompt"`
	NoResponseEndMessage     string `json:"no_response_end_message"`
}

func (c *Client) GetCallConfig(callID string) (*CallConfig, error) {
	var result CallConfig
	err := c.get(fmt.Sprintf("/%s/%s/config%s", c.callsPath, callID, c.configQuery), &result)
	return &result, err
}

func (c *Client) Turn(callID, transcript string) (*TurnResponse, error) {
	var raw json.RawMessage
	err := c.post(fmt.Sprintf("/%s/%s/turn", c.callsPath, callID), map[string]string{"transcript": transcript}, &raw)
	var result TurnResponse
	if err == nil {
		err = json.Unmarshal(raw, &result)
		result.Raw = raw
	}
	return &result, err
}

// TurnStream is Turn, but the backend sends the reply sentence by sentence as the
// LLM writes it (newline-delimited JSON, see call_turn_stream in internal.py).
// onSentence is called, in order, for each speakable sentence; the returned
// TurnResponse is the final "done" line, whose ReplyText is the full reply.
// streamed counts the sentences delivered, so the caller knows whether anything
// was already spoken when an error comes back.
func (c *Client) TurnStream(callID, transcript string, onSentence func(string)) (result *TurnResponse, streamed int, err error) {
	payload, err := json.Marshal(map[string]string{"transcript": transcript})
	if err != nil {
		return nil, 0, fmt.Errorf("marshal request: %w", err)
	}
	req, err := http.NewRequest(http.MethodPost,
		fmt.Sprintf("%s/%s/%s/turn/stream", c.baseURL, c.callsPath, callID), bytes.NewReader(payload))
	if err != nil {
		return nil, 0, fmt.Errorf("build request: %w", err)
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("X-Internal-Token", c.internalToken)
	resp, err := c.httpClient.Do(req)
	if err != nil {
		return nil, 0, fmt.Errorf("call backend %s: %w", req.URL.Path, err)
	}
	defer resp.Body.Close()
	if resp.StatusCode >= 400 {
		body, _ := io.ReadAll(io.LimitReader(resp.Body, 2048))
		return nil, 0, fmt.Errorf("backend %s failed (%d): %s", req.URL.Path, resp.StatusCode, string(body))
	}

	reader := bufio.NewReader(resp.Body)
	for {
		line, readErr := reader.ReadBytes('\n')
		if trimmed := bytes.TrimSpace(line); len(trimmed) > 0 {
			var event struct {
				Type   string `json:"type"`
				Text   string `json:"text"`
				Detail string `json:"detail"`
			}
			if err := json.Unmarshal(trimmed, &event); err != nil {
				return nil, streamed, fmt.Errorf("decode stream line: %w", err)
			}
			switch event.Type {
			case "sentence":
				if strings.TrimSpace(event.Text) != "" {
					streamed++
					onSentence(event.Text)
				}
			case "done":
				var done TurnResponse
				if err := json.Unmarshal(trimmed, &done); err != nil {
					return nil, streamed, fmt.Errorf("decode done line: %w", err)
				}
				done.Raw = json.RawMessage(append([]byte(nil), trimmed...))
				return &done, streamed, nil
			case "error":
				return nil, streamed, fmt.Errorf("backend turn failed: %s", event.Detail)
			}
		}
		if readErr != nil {
			if errors.Is(readErr, io.EOF) {
				return nil, streamed, errors.New("stream ended before the turn finished")
			}
			return nil, streamed, fmt.Errorf("read stream: %w", readErr)
		}
	}
}

// CanStream is false for browser test sessions: the backend's test engine
// simulates tools and has no streaming variant.
func (c *Client) CanStream() bool { return c.callsPath == "calls" }

// RetractReply deletes an AI reply the caller never heard (they spoke over it),
// keeping the transcript and the LLM's history truthful.
func (c *Client) RetractReply(callID, messageID string) error {
	if messageID == "" {
		return nil
	}
	req, err := http.NewRequest(http.MethodDelete,
		fmt.Sprintf("%s/%s/%s/messages/%s", c.baseURL, c.callsPath, callID, messageID), nil)
	if err != nil {
		return fmt.Errorf("build request: %w", err)
	}
	return c.do(req, nil)
}

// End reports a call Twilio already ended naturally (the caller hung up).
func (c *Client) End(callID, status string, durationSeconds int) error {
	return c.post(fmt.Sprintf("/%s/%s/end", c.callsPath, callID), map[string]any{
		"status":           status,
		"duration_seconds": durationSeconds,
	}, nil)
}

// Finalize asks the backend to actively end a still-live call (max duration
// exceeded, or no-response retries exhausted) — as opposed to End, which
// just records that Twilio already ended it.
func (c *Client) Finalize(callID, action, reason string) error {
	return c.post(fmt.Sprintf("/%s/%s/finalize", c.callsPath, callID), map[string]string{
		"action": action,
		"reason": reason,
	}, nil)
}

func (c *Client) post(path string, body any, out any) error {
	payload, err := json.Marshal(body)
	if err != nil {
		return fmt.Errorf("marshal request: %w", err)
	}

	req, err := http.NewRequest(http.MethodPost, c.baseURL+path, bytes.NewReader(payload))
	if err != nil {
		return fmt.Errorf("build request: %w", err)
	}
	req.Header.Set("Content-Type", "application/json")
	return c.do(req, out)
}

func (c *Client) get(path string, out any) error {
	req, err := http.NewRequest(http.MethodGet, c.baseURL+path, nil)
	if err != nil {
		return fmt.Errorf("build request: %w", err)
	}
	return c.do(req, out)
}

func (c *Client) do(req *http.Request, out any) error {
	req.Header.Set("X-Internal-Token", c.internalToken)

	resp, err := c.httpClient.Do(req)
	if err != nil {
		return fmt.Errorf("call backend %s: %w", req.URL.Path, err)
	}
	defer resp.Body.Close()

	respBody, err := io.ReadAll(resp.Body)
	if err != nil {
		return fmt.Errorf("read backend response: %w", err)
	}
	if resp.StatusCode >= 400 {
		return fmt.Errorf("backend %s failed (%d): %s", req.URL.Path, resp.StatusCode, string(respBody))
	}
	if out != nil {
		if err := json.Unmarshal(respBody, out); err != nil {
			return fmt.Errorf("decode backend response: %w", err)
		}
	}
	return nil
}
