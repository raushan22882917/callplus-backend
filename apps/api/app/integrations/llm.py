"""A workspace's own language model: any service that speaks the OpenAI chat-completions format."""

from dataclasses import dataclass

import httpx


# Presets for the "bring your own language model" form. Every one speaks the OpenAI chat-completions format, so
# one client serves them all. The model name is editable because providers rename models often.
LLM_PRESETS: dict[str, dict[str, str]] = {
    "openai": {"label": "OpenAI", "url": "https://api.openai.com/v1/chat/completions", "model": "gpt-4.1-mini"},
    "gemini": {
        "label": "Google Gemini",
        "url": "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions",
        "model": "gemini-2.0-flash",
    },
    "groq": {"label": "Groq", "url": "https://api.groq.com/openai/v1/chat/completions", "model": "llama-3.3-70b-versatile"},
    "mistral": {"label": "Mistral", "url": "https://api.mistral.ai/v1/chat/completions", "model": "mistral-small-latest"},
    "deepseek": {"label": "DeepSeek", "url": "https://api.deepseek.com/chat/completions", "model": "deepseek-chat"},
    "together": {
        "label": "Together AI",
        "url": "https://api.together.xyz/v1/chat/completions",
        "model": "meta-llama/Llama-3.3-70B-Instruct-Turbo",
    },
    "openrouter": {"label": "OpenRouter", "url": "https://openrouter.ai/api/v1/chat/completions", "model": "openai/gpt-4.1-mini"},
    "custom": {"label": "Another OpenAI-compatible service", "url": "", "model": ""},
}



@dataclass(frozen=True)
class LlmEndpoint:
    """Where and how to ask a model for a reply. Passed as `api_key` to sarvam.chat_completion(_stream)."""

    url: str
    model: str
    api_key: str
    label: str = "your language model"


class LlmError(Exception):
    pass


def endpoint_from(preset: str, *, api_key: str, model: str | None, base_url: str | None) -> LlmEndpoint:
    spec = LLM_PRESETS.get(preset)
    if spec is None:
        raise LlmError(f"Unknown provider. Choose one of: {', '.join(LLM_PRESETS)}")
    url = (base_url or spec["url"]).strip()
    chosen = (model or spec["model"]).strip()
    if not url.startswith("https://"):
        raise LlmError("The address must start with https://")
    if not chosen:
        raise LlmError("Enter the model name to use.")
    return LlmEndpoint(url=url, model=chosen, api_key=api_key.strip(), label=spec["label"])


def _explain(status: int, text: str, label: str) -> str:
    if status in (401, 403):
        return f"{label} did not accept this key. Check that it is correct and has access to the model."
    if status == 404:
        return f"{label} says the model or address was not found. Check the model name and address."
    if status == 429:
        return f"{label} is rate-limiting or out of credit for this key."
    return f"{label} returned an error ({status}): {text[:200]}"


async def check_endpoint(endpoint: LlmEndpoint) -> None:
    """Ask for one word before saving, so a wrong key or model is caught now and not in the middle of a call."""
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            response = await client.post(
                endpoint.url,
                headers={"Authorization": f"Bearer {endpoint.api_key}"},
                json={
                    "model": endpoint.model,
                    "messages": [{"role": "user", "content": "Reply with the single word OK."}],
                    "max_tokens": 16,
                },
            )
    except httpx.HTTPError as exc:
        raise LlmError(f"Could not reach {endpoint.label}: {exc}") from exc
    if response.status_code >= 400:
        raise LlmError(_explain(response.status_code, response.text, endpoint.label))
    try:
        choices = response.json().get("choices") or []
    except ValueError:
        choices = []
    if not choices:
        raise LlmError(f"{endpoint.label} answered, but not in the chat-completions format. Check the address.")
