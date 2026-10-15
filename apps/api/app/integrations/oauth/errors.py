"""Errors raised by the OAuth framework. `code` is a stable machine-readable
identifier surfaced to the frontend (`?error=<code>` or JSON `detail.code`)."""


class OAuthError(Exception):
    def __init__(self, code: str, message: str | None = None, *, status_code: int = 400):
        self.code = code
        self.message = message or code.replace("_", " ")
        self.status_code = status_code
        super().__init__(self.message)

    def as_detail(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message}


class ProviderNotConfigured(OAuthError):
    def __init__(self, provider: str):
        super().__init__(
            "provider_not_configured",
            f"The platform has no OAuth client credentials configured for {provider}. "
            "Ask the platform administrator to set them (see docs/OAUTH_SETUP.md).",
        )
