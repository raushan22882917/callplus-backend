"""The packs must keep covering what they cost to serve.

The prices and allowances in `packs.CATALOGUE` are chosen with `scripts/cost_model.py`. This test runs that model over the
catalogue as it is now, so a price cut or a bigger allowance that would make a pack lose money fails here instead of
showing up later on an invoice. When a provider's price or the exchange rate moves, update the model first.
"""

import importlib.util
from pathlib import Path

import pytest
from app.services import packs

MODEL_PATH = Path(__file__).resolve().parents[1] / "scripts" / "cost_model.py"


@pytest.fixture(scope="module")
def model():
    spec = importlib.util.spec_from_file_location("cost_model", MODEL_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _as_model_pack(model, code: str):
    pack = packs.CATALOGUE[code]
    allowance = pack.allowances
    return model.Pack(
        pack.name,
        float(pack.price_inr),
        minutes=allowance.get("ai_minutes", 0),
        whatsapp=allowance.get("whatsapp_messages", 0),
        emails=allowance.get("email_messages", 0),
        numbers=model.PACK_NUMBERS if code == "call" else 0,
    )


@pytest.mark.parametrize("code", packs.CODES)
def test_no_customer_can_cost_more_than_they_pay_even_in_the_worst_case(model, code: str) -> None:
    pack = _as_model_pack(model, code)
    inbound = 0.0 if pack.minutes else model.INBOUND_SHARE  # a call pack is stressed with every call outbound
    assert model.pack_margin(pack, 1.0, worst=True, inbound_share=inbound) >= 0, (
        f"{pack.name} loses money at full use in the worst case; raise the price or cut the allowance"
    )


@pytest.mark.parametrize("code", packs.CODES)
def test_every_pack_is_profitable_when_used_normally(model, code: str) -> None:
    pack = _as_model_pack(model, code)
    assert model.pack_margin(pack, 0.6) >= 0.40  # at 60% use at least 40% of the price is left


def test_the_email_pack_keeps_the_stress_margin_that_pays_for_hosting_and_support(model) -> None:
    pack = _as_model_pack(model, "email_sms")
    # Email at Resend Pro's cost with the plan full.
    assert model.pack_margin(pack, 1.0, worst=True) >= model.STRESS_MARGIN_FLOOR


def test_the_email_allowance_is_not_over_generous_for_the_price(model) -> None:
    pack = _as_model_pack(model, "email_sms")
    spare = pack.price * (1 - model.STRESS_MARGIN_FLOOR) - pack.price * model.RAZORPAY_FEE
    assert pack.emails <= spare / model.EMAIL_INR_WORST


def test_the_email_pack_is_cheaper_than_every_paid_marketing_tool_that_covers_the_same_emails(model) -> None:
    pack = _as_model_pack(model, "email_sms")
    for name, kind, usd, emails, _ in model.EMAIL_COMPETITORS:
        if kind != "platform" or emails < pack.emails:
            continue  # a plan for fewer emails (Zoho's 500 contacts) is not a like for like comparison
        assert pack.price < model.competitor_inr((name, kind, usd, emails, "")), name


def _bundle_cost(model, *, calls_all_outbound: bool) -> float:
    return sum(
        model.pack_cost(
            _as_model_pack(model, code), 1.0, worst=True,
            inbound_share=0.0 if (code == "call" and calls_all_outbound) else model.INBOUND_SHARE,
        )
        for code in packs.CODES
    )


def test_the_bundle_discount_is_applied_as_documented(model) -> None:
    quote = packs.quote(list(packs.CODES))
    total = sum(_as_model_pack(model, code).price for code in packs.CODES)
    assert quote["amount_paise"] / 100 == pytest.approx(total * (100 - packs.bundle_discount_percent()) / 100, abs=1)


def test_the_bundle_covers_its_cost_when_every_channel_is_fully_used_at_the_dearest_message_mix(model) -> None:
    # Every pack used up, WhatsApp all marketing, email at the stress cost, calls at the
    # normal inbound and outbound mix.
    assert packs.quote(list(packs.CODES))["amount_paise"] / 100 > _bundle_cost(model, calls_all_outbound=False)


def test_the_bundle_still_covers_its_cost_in_the_single_worst_month(model) -> None:
    """One workspace using every allowance in full, every call outbound and every WhatsApp message a paid marketing
    message, all in the same month. With the Email pack priced near the market the 10% discount still leaves a margin."""
    price = packs.quote(list(packs.CODES))["amount_paise"] / 100
    cost = _bundle_cost(model, calls_all_outbound=True)
    assert price > cost
