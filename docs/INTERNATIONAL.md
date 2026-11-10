# Serving customers outside India: bring your own providers

India runs on Sarvam, which hears 22 Indian languages and English and speaks 11 of them. For the rest of the world a
workspace connects its **own** accounts, and pays those providers directly:

| Job | Indian market | Rest of the world (workspace's own key) |
|---|---|---|
| Hear the caller (speech to text) | Sarvam | **Deepgram** (nova-3, 30+ languages) |
| Speak to the caller (text to speech) | Sarvam | **ElevenLabs** (Flash v2.5, 30+ languages, thousands of voices, cloned voices) |
| Write the replies (language model) | Sarvam | **Any OpenAI-compatible model**: OpenAI, Google Gemini, Groq, Mistral, DeepSeek, Together, OpenRouter or a custom address |
| Reach the business | Exotel or Twilio numbers | Twilio numbers in other countries, or calls from the browser (see WEB_CALLS.md) |

## CallPlus's accounts or the workspace's own

For each provider (Sarvam, Deepgram, ElevenLabs, the language model) a workspace chooses where it runs, on the
Integrations page:

* **CallPlus**: the platform owner's own account. Nothing to set up. Sarvam on CallPlus is one plan minute per call
  minute. Deepgram and ElevenLabs on CallPlus add extra minutes: with `MANAGED_MINUTE_EXTRAS=deepgram:0.2,elevenlabs:1`,
  a call that hears with Deepgram and speaks with ElevenLabs on CallPlus counts 2.2 minutes per minute
  (`calls.cost_multiplier`).
* **My own account**: the workspace's key. It pays that provider. When hearing, speaking and the reply all use its own
  keys, CallPlus charges 0 AI minutes (`cost_multiplier` 0), so the plan and wallet are not billed again.

Sarvam is offered as soon as `SARVAM_API_KEY` is set. Deepgram, ElevenLabs and a separate language model stay off until
the owner lists them in `MANAGED_PROVIDERS` and sets their keys (see `deploy/digitalocean/.env.example`; `deploy.sh`
copies that local file to the server). With no choice made, a workspace's own account is used when it has one,
otherwise CallPlus's when offered. If a chosen account is not there, the other is used so a call never fails for a
setting. The Platform admin setup checklist shows whether this is on.

## For a workspace

1. **Integrations → Calls outside India**: connect Deepgram, ElevenLabs and, optionally, your own language model. Each key
   is checked with the provider before it is saved, and never shown again.
2. **AI agents → open an agent**: under *Hears callers with* and *Speaks with* choose Deepgram and ElevenLabs, pick the
   agent's language (many more are offered) and its ElevenLabs voice.
3. Take calls on a Twilio number in the caller's country, or share the browser call page (`/call/<key>`).

A different agent can serve each market: an Indian-language agent on Sarvam and a Spanish one on Deepgram and ElevenLabs
can live in the same workspace. If a key is disconnected later, agents that used it fall back to Sarvam, so a call never
goes silent.

## How it works

* `ai_agents.stt_provider` / `tts_provider` hold the choice (`sarvam` by default). The API refuses a choice the workspace
  has not connected, a language the chosen providers do not handle, and ElevenLabs without a voice
  (`app/api/v1/agents.py`).
* Keys live in the existing encrypted integration store. `app/integrations/credentials.py` resolves them:
  `speech_settings()` for hearing and speaking, `get_llm_key()` for the model.
* `GET /internal/calls/{id}/config` now carries `stt_provider`, `stt_api_key`, `stt_language`, `tts_provider`,
  `tts_api_key` and `tts_model`. The Go gateway picks Deepgram (`internal/deepgram`) or Sarvam to hear, and ElevenLabs
  (`internal/elevenlabs`) or Sarvam to speak. Both ask for 8 kHz mu-law, which a phone line plays as it is, so nothing is
  converted. ElevenLabs audio is streamed, so the first words play while the rest is still being made.
* The model call goes through `integrations/sarvam.py`: `chat_completion` and `chat_completion_stream` accept either a Sarvam
  key or an `LlmEndpoint`, so every place that asks a model for text (live calls, WhatsApp and chat replies, call analysis,
  campaigns, the test studio, QA, AI-write) uses the workspace's own model with no other change.
* An international agent gets the international phone-call rules (`voice_rules_for` in `agent_engine.py`): the same
  conversation protocol without India's register, scripts and rupees. The agent replies in the caller's language in its normal
  script, because ElevenLabs reads the words as written.

## Limits to know about

* Hearing and speaking quality, price and language coverage are the providers' own. Check a language is supported by both
  Deepgram and ElevenLabs before promising it to a customer.
* Choose a fast model for the replies: a caller is waiting. A slow or very large model will make calls feel laggy.
* WhatsApp voice notes are still transcribed by Sarvam, which covers Indian languages and English.
* The wallet and plan prices are in rupees. On their own Sarvam key, customers pay Sarvam and CallPlus does not charge
  those AI minutes. On CallPlus's Sarvam, the minutes come out of the plan or wallet. Premium providers on CallPlus
  count extra minutes (see above), so set `MANAGED_MINUTE_EXTRAS` from your real provider costs before switching them on.
* Not exercised against the live providers in automated tests (they need real keys): the clients are tested against fakes
  that follow each provider's documented protocol. Make one real test call per provider after connecting.
