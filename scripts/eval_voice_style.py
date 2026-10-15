"""Compare how human the voice agent's replies sound under two prompt variants,
using the live Sarvam fast model (needs SARVAM_API_KEY in the environment).

    PYTHONPATH=.:apps/api SARVAM_API_KEY=... uv run python scripts/eval_voice_style.py

Prints each reply side by side plus simple, mechanical markers of "robotic"
speech: stock customer-service phrases, reply length, repeated openers, and
masculine verb forms from a female voice. The numbers support a judgement; they
do not replace listening to a call.
"""

import asyncio
import os
import re
from collections import Counter

from app.integrations import sarvam
from app.services import agent_engine as ae

BUSINESS = (
    "You are the receptionist of Glow Salon in Pune. Services: haircut (Rs 400), hair colour (Rs 1500), "
    "facial (Rs 800). Open 10am-8pm every day, walk-ins welcome. "
)
CALLERS = [
    "Hi, I want to book a haircut for tomorrow evening",
    "नमस्ते, फेशियल कितने का है?",
    "what are your timings and do you take walk ins",
    "Mera appointment kal tha, aap logon ne bahut wait karaya, bohot bura experience tha",
    "haan toh kitne baje free ho aap shaam ko",
    "ok thanks bye",
    "kya aap robot ho?",
    "can I talk to a real person",
    "hair colour karwana hai, kitna lagega",
    "umm... I'm not sure, maybe next week",
    "तो फिर कल शाम छह बजे ठीक रहेगा",
    "do you do bridal makeup",
]
ROBOTIC = [
    "i understand", "certainly", "absolutely", "i apologize", "inconvenience", "as an ai", "kindly",
    "assist you", "assistance", "feel free", "anything else i can help", "how may i", "thank you for", "great choice",
    "रुचि", "सहायता", "असुविधा", "क्षमा करें",
]


def variant_old(voice):
    return BUSINESS + ae.TOOL_HONESTY_RULES + ae.VOICE_STYLE_RULES + ae.CONVERSATION_RULES


def variant_new(voice):
    return (
        BUSINESS
        + ae.TOOL_HONESTY_RULES
        + ae.VOICE_STYLE_RULES
        + ae.HUMAN_VOICE_RULES
        + ae.gender_note(voice)
        + ae.CONVERSATION_RULES
    )


async def reply(system: str, text: str, key: str) -> str:
    messages = ae.with_language_hint(
        [{"role": "system", "content": system}, {"role": "user", "content": text}], "hi-IN"
    )
    out = await sarvam.chat_completion(messages=messages, api_key=key, reasoning_effort="low")
    return ae.clean_for_speech(out["choices"][0]["message"].get("content") or "")


def score(replies: list[str], female: bool) -> dict:
    words = [len(r.split()) for r in replies]
    robotic = sum(any(p in r.lower() for p in ROBOTIC) for r in replies)
    openers = Counter(" ".join(r.split()[:2]).lower() for r in replies)
    repeated = sum(n - 1 for n in openers.values() if n > 1)
    male_verbs = sum(len(re.findall(r"(ता|ता है|गया|रहा)\s*हूँ|ता हूँ|karta hoon|gaya hoon", r)) for r in replies) if female else 0
    return {
        "avg_words": round(sum(words) / len(words), 1),
        "replies_with_stock_phrases": robotic,
        "repeated_openers": repeated,
        "masculine_forms_from_female_voice": male_verbs,
    }


async def main() -> None:
    key = os.environ["SARVAM_API_KEY"]
    voice = os.environ.get("VOICE", "priya")
    results = {}
    for name, build in (("old prompt", variant_old), ("new prompt", variant_new)):
        system = build(voice)
        results[name] = await asyncio.gather(*(reply(system, c, key) for c in CALLERS))
    for i, caller in enumerate(CALLERS):
        print(f"\nCALLER: {caller}")
        for name, replies in results.items():
            print(f"  {name:11s}: {replies[i]}")
    print()
    for name, replies in results.items():
        print(f"{name:11s}", score(replies, voice_is_female(voice)))


def voice_is_female(voice: str) -> bool:
    return ae.voice_gender(voice) == "female"


if __name__ == "__main__":
    asyncio.run(main())
