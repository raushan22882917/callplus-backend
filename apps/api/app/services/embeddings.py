"""Turning text into vectors, and into the stemmed terms retrieval compares.

The built-in embedder hashes word and word-pair features into 384 numbers. It
needs no key and no network, is deterministic, and is as good as the words
the question and the passage share; the synonym expansion below is what lets
"cost" find "price". An external embeddings API can implement the same
`Embedder` interface later without touching retrieval."""

import hashlib
import math
import re
from collections import Counter
from typing import Protocol

from packages.database.vector_support import EMBEDDING_DIM

_WORD = re.compile(r"\w+", re.UNICODE)
STOPWORDS = frozenset(
    """a an and are as at be but by can could do does did for from had has have he her his how i if in
    into is it its me my no not of on or our please she so that the their them then there these they this
    to us was we were what when where which who why will with would you your tell about""".split()
)

# Words that mean the same thing to a customer. Each group is matched as a unit.
SYNONYM_GROUPS: tuple[frozenset[str], ...] = tuple(
    frozenset(g.split())
    for g in (
        "price pricing cost costs fee fees charge charges rate rates plan plans amount how much",
        "contact support helpline reach phone call email mail",
        "refund refunds return returns reimbursement cancel cancellation",
        "hours timing timings open opening close closing schedule",
        "location address directions visit find",
        "delivery shipping shipment dispatch courier",
        "payment pay paying upi card cards netbanking",
        "warranty guarantee guaranteed",
        "appointment booking book reserve reservation slot",
        "discount offer offers coupon deal deals promotion",
        "human agent person representative staff",
    )
)


def stem(word: str) -> str:
    """Light suffix stripping so refund/refunds, booking/booked meet."""
    for suffix in ("ing", "ed", "es", "s", "ly"):
        if len(word) > len(suffix) + 2 and word.endswith(suffix):
            return word[: -len(suffix)]
    return word


_STEMMED_GROUPS = tuple(frozenset(stem(w) for w in g) for g in SYNONYM_GROUPS)


# Hindi words callers actually say, mapped onto the English synonym groups so a
# Hindi question can find an English price list. This is a word list, not a
# meaning model.
_HINDI_TO_ENGLISH = {
    "कीमत": "price",
    "दाम": "price",
    "भाव": "price",
    "समय": "hours",
    "टाइमिंग": "hours",
    "पता": "address",
    "डिलीवरी": "delivery",
    "वापसी": "refund",
    "रिफंड": "refund",
    "अपॉइंटमेंट": "appointment",
    "बुकिंग": "booking",
}


def terms(text: str) -> list[str]:
    """Distinct stemmed content words, in order."""
    for hindi, english in _HINDI_TO_ENGLISH.items():
        text = text.replace(hindi, f" {english} ")
    seen: dict[str, None] = {}
    for word in _WORD.findall(text.lower()):
        word = _HINDI_TO_ENGLISH.get(word, word)
        if len(word) > 1 and word not in STOPWORDS:
            seen.setdefault(stem(word), None)
    return list(seen)


def synonyms_of(stemmed_term: str) -> frozenset[str]:
    """The stems that mean the same as this one (including itself)."""
    out = {stemmed_term}
    for group in _STEMMED_GROUPS:
        if stemmed_term in group:
            out |= group
    return frozenset(out)


def expand(text: str) -> list[str]:
    """The question's stems plus their synonyms, for search and for embedding."""
    out: dict[str, None] = {}
    for t in terms(text):
        for s in sorted(synonyms_of(t)):
            out.setdefault(s, None)
    return list(out)


class Embedder(Protocol):
    name: str
    dim: int

    def embed(self, texts: list[str]) -> list[list[float]]: ...


class HashingEmbedder:
    name = "hash-384-v1"
    dim = EMBEDDING_DIM

    def _vector(self, text: str) -> list[float]:
        words = [stem(w) for w in _WORD.findall(text.lower()) if len(w) > 1 and w not in STOPWORDS]
        features: Counter[str] = Counter(words)
        features.update(f"{a}_{b}" for a, b in zip(words, words[1:], strict=False))  # word pairs
        vec = [0.0] * self.dim
        for feature, count in features.items():
            h = int.from_bytes(hashlib.blake2b(feature.encode(), digest_size=8).digest(), "big")
            weight = (1.0 + math.log(count)) * (0.5 if "_" in feature else 1.0)
            vec[h % self.dim] += weight if (h >> 63) & 1 else -weight
        norm = math.sqrt(sum(v * v for v in vec))
        return [v / norm for v in vec] if norm else vec

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._vector(t) for t in texts]


_embedder: Embedder = HashingEmbedder()


def get_embedder() -> Embedder:
    return _embedder
