package session

import (
	"crypto/hmac"
	"crypto/sha256"
	"encoding/hex"
	"strconv"
	"time"

	"github.com/gorilla/websocket"
)

// A web call is a visitor talking to the AI agent from a web page. The API creates the call and hands the
// browser a short-lived signed address (see app/services/web_call.py); the browser then speaks the same
// Media Streams protocol as a phone carrier. The signature is what lets a stranger's browser start a
// session, so it is checked here before the WebSocket is even upgraded.

// VerifyWebCall reports whether sig is the right signature for this call and expiry (unix seconds) and the
// address has not expired. The key is the shared INTERNAL_SERVICE_TOKEN.
func VerifyWebCall(key, callID string, exp int64, sig string, now time.Time) bool {
	if callID == "" || now.Unix() > exp {
		return false
	}
	want := webCallMAC(key, callID, exp)
	return hmac.Equal([]byte(want), []byte(sig))
}

func webCallMAC(key, callID string, exp int64) string {
	mac := hmac.New(sha256.New, []byte(key))
	mac.Write([]byte("web-call:" + callID + ":" + strconv.FormatInt(exp, 10)))
	return hex.EncodeToString(mac.Sum(nil))
}

// NewWebCall starts a session for a call made from a web page. The call already exists in the backend; its
// id comes from the signed address, never from anything the browser sends.
func NewWebCall(conn *websocket.Conn, cfg Config, callID string) *Session {
	s := New(conn, cfg)
	s.webCallID = callID
	return s
}
