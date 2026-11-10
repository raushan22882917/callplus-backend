# Calls from a web page (browser voice)

A visitor can talk to an AI agent from any browser, anywhere in the world, with no phone and no carrier. This is how
international callers reach you without paying international rates.

## How it works

1. The page asks the API to start a call: `POST /api/v1/channels/webcall/{public_key}/start`. The key is the
   **web chat** channel's public key (the same one the chat widget uses).
2. The API checks that voice is turned on for that channel, the website is allowed, the visitor is under the rate
   limit and the plan still has minutes, then creates a normal `Call` row (inbound, `from_number = "web"`).
3. It returns a short-lived signed address on the realtime gateway: `wss://<REALTIME_DOMAIN>/ws/web-call?call=…&exp=…&sig=…`.
   The signature is an HMAC of the call id and expiry with `INTERNAL_SERVICE_TOKEN`; the gateway checks it before it
   accepts the WebSocket (`apps/realtime/internal/session/webcall.go`, `app/services/web_call.py`).
4. The browser (`src/components/web-call/`) sends microphone audio as 8 kHz mu-law frames in the Twilio Media Streams
   protocol and plays the agent's audio back. From here the call is an ordinary call: speech recognition, the
   agent, tools, minutes and billing, transcripts and analysis all work as for a phone call.

A web call has no phone leg, so a transfer to a human becomes a polite end of call (the transcript and summary are saved
for follow-up).

## Turning it on for a workspace

* Channels → Website live chat → tick **Let visitors talk to the AI agent by voice from their browser**, and pick an agent.
* Share `https://<your site>/call/<public key>`, or embed it in an iframe on your own website.
* Optionally limit **Allowed websites**. Starts are limited to 4 per visitor and 60 per key every 10 minutes.

## The "Talk to our AI" button on the marketing site

Set `WEB_CALL_DEMO_KEY` (in `deploy/digitalocean/.env`, because `deploy.sh` copies that file to the server) to a web chat
channel's public key that has voice turned on, then redeploy. Leave it empty to hide the button.

## Costs and limits

* The visitor pays nothing. You pay the speech and language-model cost (about ₹2.9 a minute) and no carrier cost.
* Calls use the plan's AI minutes and obey the plan's per-call cap and the agent's own maximum duration.
* Quality is telephone quality (8 kHz), the same as a phone call.
* Speech recognition and voices are Sarvam's, so Indian languages and English work. Other languages need another speech provider.
