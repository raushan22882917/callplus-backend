"""The platform's own provider accounts ("CallPlus"), offered to workspaces next to bringing their own.

A workspace chooses, per provider, between **CallPlus** (nothing to set up; minutes come out of the plan or
wallet) and **its own account** (it pays the provider, and CallPlus does not charge those AI minutes again).
Sarvam is offered as soon as SARVAM_API_KEY is set. Deepgram, ElevenLabs and a separate language model stay
off until they are listed in MANAGED_PROVIDERS, because those minutes cost more.
"""

from app.core.config import get_settings
from app.integrations.llm import LlmEndpoint

PROVIDERS = ("sarvam", "deepgram", "elevenlabs", "llm")


def _listed() -> set[str]:
    return {p.strip().lower() for p in get_settings().managed_providers.split(",") if p.strip()}


def platform_key(provider: str) -> str:
    s = get_settings()
    return {
        "sarvam": s.sarvam_api_key,
        "deepgram": s.deepgram_api_key,
        "elevenlabs": s.elevenlabs_api_key,
        "llm": s.managed_llm_api_key,
    }.get(provider, "")


def platform_llm_endpoint() -> LlmEndpoint | None:
    s = get_settings()
    if s.managed_llm_api_key and s.managed_llm_url and s.managed_llm_model:
        return LlmEndpoint(
            url=s.managed_llm_url, model=s.managed_llm_model, api_key=s.managed_llm_api_key, label=s.managed_llm_label
        )
    return None


def available(provider: str) -> bool:
    """Is the platform's account offered for this provider?

    Sarvam is the included price: the platform key alone is enough. Deepgram, ElevenLabs and a separate
    language model must also be listed. "llm" with no separate model means Sarvam's model on that key.
    """
    if provider == "sarvam":
        return bool(platform_key("sarvam"))
    if provider not in _listed():
        return False
    if provider == "llm":
        return platform_llm_endpoint() is not None or available("sarvam")
    return bool(platform_key(provider))


def extra_minutes(provider: str) -> float:
    """Extra minutes counted per call minute when this provider is used on the platform's account."""
    extras: dict[str, float] = {}
    for part in get_settings().managed_minute_extras.split(","):
        name, _, value = part.partition(":")
        try:
            extras[name.strip().lower()] = max(0.0, float(value))
        except ValueError:
            continue
    return extras.get(provider, 0.0)
