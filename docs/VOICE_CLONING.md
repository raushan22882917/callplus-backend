# Cloning a voice for an AI agent

A person can have their voice cloned with their permission, and an agent can then speak with it on every call, phone
and browser. There are two providers, each on the workspace's own account or on CallPlus's:

| Provider | Best for | Needs |
|---|---|---|
| **ElevenLabs** instant clone | any language it supports, worldwide | an ElevenLabs plan that allows instant cloning |
| **Sarvam** instant clone (beta) | Indian languages and English | voice cloning enabled on the Sarvam account |

## How a business uses it

1. **AI agents → open an agent → Clone a voice** (under Voice).
2. Choose **ElevenLabs** or **Sarvam**, name the voice, say whose voice it is and, for Sarvam, the language it will speak.
   Tick the consent statement.
3. Read the passage aloud in a quiet room (30 to 60 seconds is best, 20 is the minimum), or upload a recording.
   Listen back, then **Create voice**.
4. The voice appears in the agent's voice list as "(cloned)". Choose it (or press **Use this voice**) and save. Choosing a
   Sarvam voice also moves the agent to a language that voice speaks. **My voices** lists, plays and
   deletes the workspace's voices; a voice an agent still uses cannot be deleted.

## How it works

* `POST /api/v1/voice-clones` (multipart) sends the recording to ElevenLabs (`POST /v1/voices/add`) and stores the
  returned voice id in `voice_clones` with the speaker's name, the consent text and its version, the time and who made
  it. The audio is not kept. Preview and delete call ElevenLabs with the account the voice was made on.
* The voice id is what the agent already uses to speak (`ai_agents.voice`). An ElevenLabs voice goes through the existing
  ElevenLabs path (`apps/realtime/internal/elevenlabs`). A Sarvam cloned voice has an id starting `svc-`; the gateway then
  speaks it through Sarvam's cloning endpoint (`POST /voices/clone/stream`, `internal/sarvam/clonetts.go`). It asks for
  headerless 16-bit audio at 8 kHz and converts it to the mu-law a phone line plays, because Sarvam's docs do not say how
  its own mu-law output is framed.
* A cloned voice is always reached with the account it was made on (`key_source`), even if the workspace later prefers
  the other account, and an agent never speaks with another workspace's clone.
* The consent statement and passage are in `app/services/voice_clones.py` (`CONSENT_VERSION`). Change the wording and
  bump the version: the API refuses a request that agrees to an older one.

## Rules built in

* **Consent is required** and stored. Creation and deletion are written to the audit log.
* **Whose voices a workspace may use.** On CallPlus's shared ElevenLabs account the voice list shows only ElevenLabs'
  premade voices and the workspace's own clones, and an agent may only be given those. A workspace can never use
  another's clone, even by pasting its voice id, and a call falls back to Sarvam if an agent holds a voice it may not use.
* **Limits.** `VOICE_CLONES_MANAGED_CAP` (default 2) clones per workspace on CallPlus's account, and
  `VOICE_CLONES_OWN_CAP` (default 25) on a workspace's own. Each clone uses a voice slot on the account it was made on,
  and ElevenLabs limits slots by plan. At most 5 clones an hour per workspace. Recordings: 20 seconds to 10 minutes, up to 25 MB.
* **Honesty.** Agents are told to say they are an AI when asked, and callers hear the disclosure at the start of a call.
  A cloned human voice makes that more important, not less.

## Before you offer it to customers

* **Check the plan.** Instant cloning needs a paid ElevenLabs plan and each plan has a voice-slot limit; sources disagree on
  the exact plan, so confirm on ElevenLabs' pricing page. Sarvam's docs do not say how cloning is enabled on an account
  (the API needs a `text_to_speech_voice_cloning` capability; the starter plan keeps up to 50 voices). A refused clone
  shows the reason in the dialog.
* **Verification.** ElevenLabs may ask the speaker to verify the voice (`requires_verification`). The voice is then marked
  "needs verification" and the speaker finishes it on ElevenLabs.
* **Languages and quality.** Test the clone with the fast model in the languages you promise before selling it.
  Hindi and other Indian languages on cloned voices are not confirmed in ElevenLabs' docs.
* **Legal.** A voice can be personal data and people have rights over their likeness; rules differ by country. Have a
  lawyer review the consent wording and how you describe this feature.

## Not built yet

* Professional voice cloning (30 minutes or more of audio, Creator plan, the owner's own voice only, verification by
  captcha, hours of training).
* Sarvam's WebSocket voice endpoint (the gateway uses the simpler HTTP stream). Sarvam voices cannot be verified by a
  voice captcha: the API has no consent field, so CallPlus records the consent itself.
* Not tried against the live Sarvam cloning API: it is a beta and needs the capability above. Make one real clone and one
  real call before offering it.
