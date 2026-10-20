"""Decides when a live call is over so the agent hangs up after its goodbye
instead of leaving the line open for the caller to end ("bye" three times).

A caller who explicitly asks to end the call ("disconnect the call please",
"hang up") is obeyed at once, and the agent's reply is just played first.

Otherwise it is deliberately conservative: it hangs up only when BOTH the caller is closing
(a goodbye, thanks or "nothing else") AND the agent's own reply is a short
goodbye. A caller saying "no" mid-booking, or an agent saying "bye" too early,
each fail one side of the test and the call carries on.
"""

import re

# Not \w: Devanagari vowel signs aren't word characters, so it would split Hindi words.
_WORD = re.compile(r"[^\s.,!?;:।\-\"()]+")

# Words that can make up a closing line from the caller.
_CLOSING_WORDS = frozenset(
    """bye goodbye thanks thank you thankyou ok okay alright fine great perfect cool nice good
    nothing no nope nah that thats that's all else more nahi nahin nhi bas bs thik theek
    take care see later day have a hai है ठीक बस नहीं नही धन्यवाद शुक्रिया थैंक्स अलविदा बाय हाँ जी
    nothing's""".split()
)
# At least one of these must be present: "okay" or "good" alone is not a goodbye.
_CLOSING_ANCHORS = frozenset(
    """bye goodbye thanks thank thankyou nothing no nope nah nahi nahin nhi bas bs
    बस नहीं नही धन्यवाद शुक्रिया थैंक्स अलविदा बाय all""".split()
)

_FAREWELL_REPLY = re.compile(
    r"\b(bye|goodbye|take care|have a (good|great|nice|lovely) (day|evening|night)|talk soon)\b"
    r"|अलविदा|फिर मिलेंगे|ध्यान रखिए",
    re.IGNORECASE,
)
# An explicit instruction to end the call. These hang up on their own, without
# waiting for the agent to say a tidy goodbye first.
_END_REQUEST = re.compile(
    r"\b(disconnect|hang\s?up|hangup|end|cut|drop|terminate)\s+(the\s+|this\s+|my\s+|that\s+)?(call|phone|line)\b"
    r"|\b(disconnect|hang\s?up|hangup)\b(\s+(it|please|now|karo|kar\s?do))?\s*[.!]?\s*$"
    r"|\b(call|phone|line|कॉल|फोन|फ़ोन)\s+(cut|kat|band|disconnect)\s+(kar|karo|kijiye|kardo)"
    r"|(कॉल|फोन|फ़ोन)\s+(कट|बंद|डिस्कनेक्ट)\s+(कर|करो|कीजिए|कीजिये)"
    r"|(फोन|फ़ोन)\s+रख",
    re.IGNORECASE,
)
# "don't hang up", "do not disconnect": the opposite request.
_NEGATED = re.compile(
    r"\b(don'?t|do\s+not|dont|never|not|mat|nahi|nahin)\b(\s+\w+){0,2}\s+"
    r"(disconnect|hang\s?up|hangup|end|cut|drop)\b",
    re.IGNORECASE,
)
_MAX_CALLER_WORDS = 8
_MAX_REQUEST_WORDS = 14
_MAX_REPLY_WORDS = 20


def caller_is_closing(transcript: str) -> bool:
    words = [w.lower().strip("'") for w in _WORD.findall(transcript or "")]
    if not words or len(words) > _MAX_CALLER_WORDS:
        return False
    if not all(w in _CLOSING_WORDS for w in words):
        return False
    return any(w in _CLOSING_ANCHORS for w in words)


def caller_wants_to_end(transcript: str) -> bool:
    """The caller explicitly asked to end or disconnect the call."""
    text = (transcript or "").strip()
    if not text or len(_WORD.findall(text)) > _MAX_REQUEST_WORDS:
        return False
    return _END_REQUEST.search(text) is not None and _NEGATED.search(text) is None


def reply_is_farewell(reply: str) -> bool:
    text = (reply or "").strip()
    if not text or "?" in text or "？" in text:
        return False
    if len(_WORD.findall(text)) > _MAX_REPLY_WORDS:
        return False
    return _FAREWELL_REPLY.search(text) is not None


def conversation_is_over(*, transcript: str, reply: str) -> bool:
    if caller_wants_to_end(transcript):
        return True
    return caller_is_closing(transcript) and reply_is_farewell(reply)
