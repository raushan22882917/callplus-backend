"""One place to build the outbound HTTP client, so every OAuth/provider call
shares the same timeout and tests can swap the transport (httpx.MockTransport)
without touching the network."""

import httpx

HTTP_TIMEOUT = 10.0

# Tests install an httpx.MockTransport here; production leaves it None.
_transport_override: httpx.AsyncBaseTransport | None = None


def set_transport(transport: httpx.AsyncBaseTransport | None) -> None:
    global _transport_override
    _transport_override = transport


def new_client(timeout: float = HTTP_TIMEOUT) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=timeout, follow_redirects=False, transport=_transport_override
    )
