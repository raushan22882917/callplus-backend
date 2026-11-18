from app.integrations import email_client
from app.integrations.email_client import email_setup_warnings


class _S:
    def __init__(self, **kw):
        self.email_api_key = ""
        self.email_api_provider = "resend"
        self.email_resend_webhook_secret = ""
        self.smtp_host = ""
        self.smtp_from_email = "no-reply@example.com"
        self.__dict__.update(kw)


def _with(monkeypatch, **kw):
    monkeypatch.setattr(email_client, "get_settings", lambda: _S(**kw))
    return email_setup_warnings()


def test_nothing_configured_is_reported(monkeypatch):
    assert "not configured" in _with(monkeypatch)[0]


def test_gmail_smtp_is_flagged_for_free_address_and_blocked_ports(monkeypatch):
    out = " ".join(_with(monkeypatch, smtp_host="smtp.gmail.com", smtp_from_email="me@gmail.com"))
    assert "gmail.com" in out and "EMAIL_API_KEY" in out


def test_resend_without_webhook_secret_is_flagged(monkeypatch):
    out = _with(monkeypatch, email_api_key="k", smtp_from_email="no-reply@mail.acme.in")
    assert len(out) == 1 and "WEBHOOK_SECRET" in out[0]


def test_a_complete_setup_has_no_warnings(monkeypatch):
    assert _with(monkeypatch, email_api_key="k", email_resend_webhook_secret="s", smtp_from_email="n@mail.acme.in") == []
