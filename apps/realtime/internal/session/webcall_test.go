package session

import (
	"testing"
	"time"
)

// The signature is computed by the API in Python (hmac sha256 over "web-call:<id>:<exp>"); this vector
// keeps the two sides in step.
const (
	wcKey = "unit-test-internal-token-123456"
	wcID  = "11111111-2222-3333-4444-555555555555"
	wcExp = int64(1700000300)
	wcSig = "198e5f0f68456898c4d615f287d3017665797162ed7e14a27efa4d028f1b35d5"
)

func TestVerifyWebCallAcceptsTheApisSignature(t *testing.T) {
	if !VerifyWebCall(wcKey, wcID, wcExp, wcSig, time.Unix(1700000000, 0)) {
		t.Fatal("a valid signature was rejected")
	}
}

func TestVerifyWebCallRejectsTamperingAndExpiry(t *testing.T) {
	now := time.Unix(1700000000, 0)
	cases := map[string]bool{
		"other call": VerifyWebCall(wcKey, "99999999-2222-3333-4444-555555555555", wcExp, wcSig, now),
		"later exp":  VerifyWebCall(wcKey, wcID, wcExp+1, wcSig, now),
		"wrong key":  VerifyWebCall("another-key-another-key-123456", wcID, wcExp, wcSig, now),
		"empty sig":  VerifyWebCall(wcKey, wcID, wcExp, "", now),
		"expired":    VerifyWebCall(wcKey, wcID, wcExp, wcSig, time.Unix(1700000301, 0)),
		"no call id": VerifyWebCall(wcKey, "", wcExp, wcSig, now),
		"junk sig":   VerifyWebCall(wcKey, wcID, wcExp, "zz", now),
	}
	for name, ok := range cases {
		if ok {
			t.Errorf("%s: should have been rejected", name)
		}
	}
}
