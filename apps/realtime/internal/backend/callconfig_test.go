package backend

import (
	"encoding/json"
	"testing"
)

// The API tells the gateway which provider hears and speaks for a call. Older API versions send nothing, which
// must read as "use Sarvam".
func TestCallConfigCarriesSpeechProviders(t *testing.T) {
	var c CallConfig
	body := `{"sarvam_api_key":"s","language":"fr","voice":"v1","stt_provider":"deepgram","stt_api_key":"dg","stt_language":"fr","tts_provider":"elevenlabs","tts_api_key":"xi","tts_model":"eleven_flash_v2_5"}`
	if err := json.Unmarshal([]byte(body), &c); err != nil {
		t.Fatal(err)
	}
	if c.STTProvider != "deepgram" || c.STTAPIKey != "dg" || c.STTLanguage != "fr" {
		t.Errorf("stt not decoded: %+v", c)
	}
	if c.TTSProvider != "elevenlabs" || c.TTSAPIKey != "xi" || c.TTSModel != "eleven_flash_v2_5" || c.Voice != "v1" {
		t.Errorf("tts not decoded: %+v", c)
	}

	var old CallConfig
	if err := json.Unmarshal([]byte(`{"sarvam_api_key":"s","language":"hi-IN","voice":"priya"}`), &old); err != nil {
		t.Fatal(err)
	}
	if old.STTProvider != "" || old.TTSProvider != "" {
		t.Errorf("a config without providers must leave them empty (meaning Sarvam), got %q / %q", old.STTProvider, old.TTSProvider)
	}
}
