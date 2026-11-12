"""Credit prices: India, a workspace's own line, and calls to other countries."""

from app.services.credits import destination, is_india, price_card, quote_call
from app.services.wallet import OWN_LINE_PAISE, credit_prices


def _quote(**over):
    base = dict(
        direction="outbound",
        duration_seconds=60,
        cost_multiplier=1,
        from_number="+14155550100",
        to_number="+919876543210",
        exotel_numbers=set(),
        inbound_rate=450,
        outbound_rate=800,
        own_line_paise=OWN_LINE_PAISE,
    )
    base.update(over)
    return quote_call(**base)


def test_one_credit_is_one_rupee_and_india_keeps_the_top_up_rate():
    cost, text = _quote(duration_seconds=61, direction="inbound", to_number="+912233445566", from_number="+919800000000")
    assert cost == 900 and text == "Inbound call, 2 min"
    cost, text = _quote()
    assert cost == 800 and text == "Outbound call, 1 min"


def test_browser_and_exotel_use_the_speech_rate_only():
    cost, text = _quote(direction="outbound", from_number="web", to_number="web", duration_seconds=120)
    assert cost == OWN_LINE_PAISE * 2 and text == "Browser call, 2 min"
    cost, text = _quote(
        from_number="+912233445566", to_number="+14155550100", exotel_numbers={"+912233445566"}
    )
    assert cost == OWN_LINE_PAISE and text == "Own-line call, 1 min"


def test_a_twilio_call_outside_india_uses_one_rate_for_every_country():
    assert destination("+14155550100") == ("another country", 1_500)
    assert destination("+971501234567") == ("another country", 1_500)
    assert destination("+491511234567") == ("another country", 1_500)
    assert is_india("+919876543210") and not is_india("+447911123456")
    cost, text = _quote(to_number="+14155550100")
    assert cost == OWN_LINE_PAISE + 1_500
    assert text == "Call to another country, 1 min"
    # Their own speech key is free, but our phone line outside India is not.
    cost, text = _quote(to_number="+971501234567", cost_multiplier=0)
    assert cost == 1_500 and text == "Call to another country, 1 min"
    cost, _ = _quote(to_number="+255700000000")
    assert cost == OWN_LINE_PAISE + 1_500


def test_premium_voice_multiplies_speech_and_leaves_the_phone_line_once():
    cost, text = _quote(to_number="+14155550100", cost_multiplier=2.2, duration_seconds=60)
    assert cost == round(OWN_LINE_PAISE * 2.2) + 1_500
    assert "x 2.2" in text
    # India on our Twilio line stays one bundled rate times the multiplier.
    cost, text = _quote(direction="inbound", cost_multiplier=2.5, duration_seconds=120)
    assert cost == round(2 * 450 * 2.5) and "x 2.5" in text


def test_the_price_card_the_page_shows_matches_what_a_call_is_charged():
    card = credit_prices(450, 800)
    assert card["credit_paise"] == 100
    assert len(card["international"]) == 1
    abroad = card["international"][0]
    assert abroad["label"] == "Calls outside India"
    assert abroad["paise"] == _quote(to_number="+14155550100")[0]
    assert abroad["paise"] == _quote(to_number="+971501234567")[0]
    india_out = next(row for row in card["india"] if row["id"] == "outbound")
    assert india_out["paise"] == 800
    assert price_card(
        inbound_paise=450, outbound_paise=800, own_line_paise=250,
        whatsapp_paise=150, sms_paise=75, email_paise=20,
    )["messages"][0]["paise"] == 150
