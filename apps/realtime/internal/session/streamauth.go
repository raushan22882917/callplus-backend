package session

import (
	"crypto/hmac"
	"crypto/sha256"
	"encoding/hex"
	"log"
	"runtime/debug"
	"strconv"
	"time"
)

// A carrier stream (/ws/twilio) is opened by whoever knows the address, so the address alone proves
// nothing. The API signs each call it hands to a carrier (see app/services/stream_auth.py) and the
// carrier passes the signature back in the stream's custom parameters. Without a valid one the session
// never reaches speech recognition, so a stranger cannot spend the platform's speech credits or pose
// as a call that exists.

// StreamAuthTTL is how long a signature stays valid: long enough for an outbound call to ring and be
// answered, short enough that a leaked one is soon useless. It only gates the connection, not the call.
const StreamAuthTTL = 10 * time.Minute

// startTimeout bounds how long a fresh socket may sit without sending its start message.
const startTimeout = 10 * time.Second

// maxFrameBytes caps one inbound message. A media frame is about 300 bytes; a start message under 2 KB.
const maxFrameBytes = 64 * 1024

// VerifyStream reports whether sig is the API's signature for this call and expiry (unix seconds) and
// the signature has not expired. The key is the shared INTERNAL_SERVICE_TOKEN.
func VerifyStream(key, callID string, exp int64, sig string, now time.Time) bool {
	if callID == "" || sig == "" || now.Unix() > exp {
		return false
	}
	return hmac.Equal([]byte(streamMAC(key, callID, exp)), []byte(sig))
}

// SignStream returns the custom parameters that prove a call was accepted by this platform. The gateway
// uses it for carriers it bridges itself (see internal/exotel), after the backend has accepted the call.
func SignStream(key, callID string, now time.Time) map[string]string {
	exp := now.Add(StreamAuthTTL).Unix()
	return map[string]string{"exp": strconv.FormatInt(exp, 10), "sig": streamMAC(key, callID, exp)}
}

func streamMAC(key, callID string, exp int64) string {
	mac := hmac.New(sha256.New, []byte(key))
	mac.Write([]byte("stream:" + callID + ":" + strconv.FormatInt(exp, 10)))
	return hex.EncodeToString(mac.Sum(nil))
}

// streamAuthorized checks the signature a carrier passed with its start message.
func (s *Session) streamAuthorized(params map[string]string) bool {
	if s.cfg.AllowUnsignedStreams {
		return true
	}
	exp, err := strconv.ParseInt(params["exp"], 10, 64)
	if err != nil {
		return false
	}
	return VerifyStream(s.cfg.InternalToken, s.callID, exp, params["sig"], time.Now())
}

// recoverPanic keeps a bug in one call from taking the whole gateway, and every other live call, down
// with it. Defer it first thing in each goroutine a session starts. The call that hit the bug is ended
// by closing its socket, which makes Run return and finalize it as usual.
func (s *Session) recoverPanic(where string) {
	if r := recover(); r != nil {
		log.Printf("[session %s] panic in %s: %v\n%s", s.callID, where, r, debug.Stack())
		_ = s.conn.Close()
	}
}
