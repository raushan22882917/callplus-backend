"""Turns a stream of LLM text deltas into whole, speakable sentences.

A live call speaks each sentence as soon as it is complete, so text must be
cut at sentence ends (. ! ? or the Devanagari danda), never inside a number
like "3.5", and stripped of what must not be read aloud (reasoning blocks,
markdown, URLs). Mirrors splitSentences in apps/realtime/internal/session.
"""

import re

_THINK_BLOCK = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
_MARKDOWN_CHARS = re.compile(r"[*_`#>~]+")
_BULLET_PREFIX = re.compile(r"^\s*(?:[-•]|\d+[.)])\s+", re.MULTILINE)
_URL = re.compile(r"https?://\S+")


def clean_for_speech(text: str) -> str:
    """Strip anything that must not be spoken: model reasoning blocks, markdown,
    list markers and raw URLs. Whitespace is collapsed to single spaces."""
    text = _THINK_BLOCK.sub("", text)
    # An unterminated <think> (truncated output) is reasoning too — drop it.
    text = re.sub(r"<think>.*", "", text, flags=re.DOTALL | re.IGNORECASE)
    text = _URL.sub("", text)
    text = _BULLET_PREFIX.sub("", text)
    text = _MARKDOWN_CHARS.sub("", text)
    return re.sub(r"\s+", " ", text).strip()


_OPEN, _CLOSE = "<think>", "</think>"
_TERMINATORS = ".!?।"


def _split_reasoning(text: str) -> tuple[str, str]:
    """(visible, held). Closed <think> blocks are dropped; an unfinished block,
    or a half-received tag at the very end, is held until more text arrives."""
    visible: list[str] = []
    while True:
        start = text.lower().find(_OPEN)
        if start == -1:
            break
        visible.append(text[:start])
        end = text.lower().find(_CLOSE, start)
        if end == -1:
            return "".join(visible), text[start:]
        text = text[end + len(_CLOSE) :]
    for n in range(min(len(_OPEN) - 1, len(text)), 0, -1):
        if _OPEN.startswith(text[-n:].lower()):
            visible.append(text[:-n])
            return "".join(visible), text[-n:]
    visible.append(text)
    return "".join(visible), ""


class SentenceStreamer:
    def __init__(self) -> None:
        self._raw = ""

    def feed(self, delta: str) -> list[str]:
        self._raw += delta
        visible, held = _split_reasoning(self._raw)
        sentences: list[str] = []
        start = 0
        for i, ch in enumerate(visible):
            if ch not in _TERMINATORS:
                continue
            following = visible[i + 1] if i + 1 < len(visible) else None
            if following is None or not following.isspace():
                continue  # "3.5", or the last char so far: wait for more text
            cleaned = clean_for_speech(visible[start : i + 1])
            if cleaned:
                sentences.append(cleaned)
            start = i + 1
        self._raw = visible[start:] + held
        return sentences

    def flush(self) -> list[str]:
        """End of the reply: whatever is left (minus unfinished reasoning)."""
        visible, _ = _split_reasoning(self._raw)
        self._raw = ""
        cleaned = clean_for_speech(visible)
        return [cleaned] if cleaned else []
