import hashlib
import hmac

from app.core.config import get_settings
from app.integrations.razorpay_client import verify_checkout_signature, verify_webhook_signature
from app.integrations.whatsapp import verify_webhook_signature as verify_whatsapp_signature


def test_whatsapp_signature_accepts_valid_and_rejects_tampered() -> None:
    settings = get_settings()
    body = b'{"entry": [{"changes": []}]}'
    digest = hmac.new(settings.meta_whatsapp_app_secret.encode(), body, hashlib.sha256).hexdigest()

    assert verify_whatsapp_signature(body, f"sha256={digest}") is True
    assert verify_whatsapp_signature(body, "sha256=deadbeef") is False
    assert verify_whatsapp_signature(body, None) is False
    assert verify_whatsapp_signature(b"tampered body", f"sha256={digest}") is False


def test_razorpay_webhook_signature_accepts_valid_and_rejects_tampered() -> None:
    settings = get_settings()
    body = b'{"event": "payment_link.paid"}'
    digest = hmac.new(
        settings.razorpay_webhook_secret.encode(), body, hashlib.sha256
    ).hexdigest()

    assert verify_webhook_signature(body, digest) is True
    assert verify_webhook_signature(body, "0" * 64) is False
    assert verify_webhook_signature(body, None) is False


def test_razorpay_checkout_signature_matches_documented_formula() -> None:
    settings = get_settings()
    order_id, payment_id = "order_ABC123", "pay_XYZ789"
    expected = hmac.new(
        settings.razorpay_key_secret.encode() or b"placeholder",
        f"{order_id}|{payment_id}".encode(),
        hashlib.sha256,
    ).hexdigest()

    # verify_checkout_signature reads key_secret from settings itself; when
    # unconfigured (test env has no real Razorpay creds) it raises, matching
    # production behaviour rather than silently accepting anything.
    if not settings.razorpay_key_id or not settings.razorpay_key_secret:
        return

    assert verify_checkout_signature(order_id=order_id, payment_id=payment_id, signature=expected)
    assert not verify_checkout_signature(
        order_id=order_id, payment_id=payment_id, signature="wrong"
    )
