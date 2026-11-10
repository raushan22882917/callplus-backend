"""The speech and language providers a workspace can bring its own account for.

India stays on Sarvam (it hears 22 Indian languages and English and speaks 11 of them). A business that serves
other countries connects the providers that are best there: Deepgram to hear and ElevenLabs to speak in dozens of
languages, and any OpenAI-compatible service (OpenAI, Google Gemini, Groq, Mistral and so on) as the model that
writes the replies. The business pays those providers directly, with its own key.

The gateway (apps/realtime) reads which provider an agent uses from its call config; see internal.py.
"""

from app.integrations.llm import LLM_PRESETS  # noqa: F401  (re-exported for callers)
from app.integrations.sarvam import SARVAM_LANGUAGES

STT_PROVIDERS: dict[str, str] = {
    "sarvam": "Sarvam (22 Indian languages and English)",
    "deepgram": "Deepgram (more than 30 languages)",
}
TTS_PROVIDERS: dict[str, str] = {
    "sarvam": "Sarvam (11 Indian languages and English)",
    "elevenlabs": "ElevenLabs (more than 30 languages, thousands of voices)",
}
SARVAM = "sarvam"
DEEPGRAM = "deepgram"
ELEVENLABS = "elevenlabs"
LLM = "llm"  # the integration row that holds the workspace's own language model

# Languages for an agent that does not use Sarvam on both sides. Codes are what Deepgram and ElevenLabs accept.
INTERNATIONAL_LANGUAGES: dict[str, str] = {
    "en-US": "English (US)",
    "en-GB": "English (UK)",
    "en-AU": "English (Australia)",
    "es": "Spanish",
    "es-MX": "Spanish (Mexico)",
    "fr": "French",
    "de": "German",
    "it": "Italian",
    "pt": "Portuguese",
    "pt-BR": "Portuguese (Brazil)",
    "nl": "Dutch",
    "pl": "Polish",
    "ru": "Russian",
    "uk": "Ukrainian",
    "tr": "Turkish",
    "ar": "Arabic",
    "he": "Hebrew",
    "ja": "Japanese",
    "ko": "Korean",
    "zh": "Chinese",
    "id": "Indonesian",
    "ms": "Malay",
    "vi": "Vietnamese",
    "th": "Thai",
    "fil": "Filipino",
    "sv": "Swedish",
    "da": "Danish",
    "no": "Norwegian",
    "fi": "Finnish",
    "el": "Greek",
    "cs": "Czech",
    "ro": "Romanian",
    "hu": "Hungarian",
    "bg": "Bulgarian",
}
ALL_LANGUAGES: dict[str, str] = {**SARVAM_LANGUAGES, **INTERNATIONAL_LANGUAGES}

def language_ok(language: str, *, stt: str, tts: str) -> bool:
    """Sarvam on both sides understands and speaks the Indian languages; anything else may use any listed language."""
    if stt == SARVAM and tts == SARVAM:
        return language in SARVAM_LANGUAGES
    return language in ALL_LANGUAGES
