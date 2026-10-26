package exotel

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"time"
)

// CallInfo is what the backend returns once it has accepted an Exotel call.
type CallInfo struct {
	CallID   string `json:"call_id"`
	TenantID string `json:"tenant_id"`
}

// StartFunc asks the backend which workspace and AI agent own this call and creates the call
// record. It is a function so tests can stand in for the backend.
type StartFunc func(streamKey, callSID, from, to string) (*CallInfo, error)

// HTTPStart returns a StartFunc that calls the backend's internal Exotel endpoint.
func HTTPStart(baseURL, internalToken string) StartFunc {
	client := &http.Client{Timeout: 15 * time.Second}
	return func(streamKey, callSID, from, to string) (*CallInfo, error) {
		body, _ := json.Marshal(map[string]string{
			"stream_key": streamKey, "call_sid": callSID, "from": from, "to": to,
		})
		req, err := http.NewRequest(http.MethodPost, baseURL+"/exotel/start", bytes.NewReader(body))
		if err != nil {
			return nil, err
		}
		req.Header.Set("Content-Type", "application/json")
		req.Header.Set("X-Internal-Token", internalToken)
		resp, err := client.Do(req)
		if err != nil {
			return nil, fmt.Errorf("call backend: %w", err)
		}
		defer resp.Body.Close()
		raw, _ := io.ReadAll(resp.Body)
		if resp.StatusCode >= 400 {
			return nil, fmt.Errorf("backend refused the call (%d): %s", resp.StatusCode, string(raw))
		}
		var info CallInfo
		if err := json.Unmarshal(raw, &info); err != nil || info.CallID == "" {
			return nil, fmt.Errorf("backend gave no call id: %s", string(raw))
		}
		return &info, nil
	}
}
