"""Unit-economics model for CallPlus plans.

Run:  python scripts/cost_model.py

Every number below is an input you can change when a provider's price moves.
Sources are in docs/BILLING.md. Items marked ASSUMPTION could not be confirmed
from the provider's own published page and should be checked in the console.
"""

from dataclasses import dataclass

# --- FX and carrier --------------------------------------------------------
USD_INR = 94.0  # the live rate the app's wallet showed on 12 Nov 2026; update when it moves
TWILIO_OUT_MOBILE_USD = 0.0496  # Twilio India outbound to mobile (published)
TWILIO_IN_USD = 0.0075  # ASSUMPTION: inbound to an Indian number, third-party figure
TWILIO_NUMBER_USD_MONTH = 2.0  # ASSUMPTION: published floor is $1.15, India local ~$2
STREAM_BUFFER_INR = 0.35  # ASSUMPTION: unconfirmed media-stream / carrier surcharge

# --- Sarvam (published, pay-as-you-go) -------------------------------------
STT_INR_PER_MIN = 0.49  # streamed for the whole call, speech or silence
TTS_INR_PER_1K_CHARS = 3.00  # bulbul:v3
LLM_IN_INR_PER_M = 15.0  # sarvam-105b input
LLM_OUT_INR_PER_M = 60.0  # sarvam-105b output

# --- conversation shape (ASSUMPTIONS, measure from real calls) -------------
TTS_CHARS_PER_CALL_MIN = 400  # agent talks ~45% of the call at ~900 chars/min
TURNS_PER_MIN = 4
LLM_IN_TOKENS_PER_TURN = 3000  # prompt + knowledge + history, with headroom
LLM_OUT_TOKENS_PER_TURN = 250  # includes reasoning tokens

# --- WhatsApp (Meta, India, ex-GST) ----------------------------------------
WA_UTILITY_INR = 0.115
WA_MARKETING_INR = 0.8631
WA_BLENDED_INR = 0.25  # ASSUMPTION: mostly in-window service + utility, some marketing

# --- payments and platform -------------------------------------------------
RAZORPAY_FEE = 0.02 * 1.18  # 2% + 18% GST on the fee
INFRA_INR_MONTH = 54.0 * USD_INR  # Railway estimated month (api, worker, realtime, web, db, redis)

INBOUND_SHARE = 0.60  # ASSUMPTION: share of minutes that are inbound


def ai_minute_cogs(outbound: bool) -> float:
    carrier = (TWILIO_OUT_MOBILE_USD if outbound else TWILIO_IN_USD) * USD_INR
    tts = TTS_CHARS_PER_CALL_MIN / 1000 * TTS_INR_PER_1K_CHARS
    llm = TURNS_PER_MIN * (
        LLM_IN_TOKENS_PER_TURN * LLM_IN_INR_PER_M + LLM_OUT_TOKENS_PER_TURN * LLM_OUT_INR_PER_M
    ) / 1_000_000
    return carrier + STREAM_BUFFER_INR + STT_INR_PER_MIN + tts + llm


INBOUND = ai_minute_cogs(False)
OUTBOUND = ai_minute_cogs(True)
BLENDED = INBOUND * INBOUND_SHARE + OUTBOUND * (1 - INBOUND_SHARE)
NUMBER_INR = TWILIO_NUMBER_USD_MONTH * USD_INR


@dataclass(frozen=True)
class Plan:
    name: str
    price: float  # per month, ex-GST
    minutes: int  # included AI minutes
    whatsapp: int  # included outbound WhatsApp messages
    numbers: int
    overage_per_min: float


PLANS = [
    Plan("Starter", 2999, 300, 1000, 2, 10.0),
    Plan("Growth", 12999, 1500, 5000, 5, 9.0),
    Plan("Scale", 54999, 6000, 25000, 20, 8.0),
]


def margin(plan: Plan, utilisation: float) -> tuple[float, float, float]:
    """(cost, profit, gross margin) when the tenant uses `utilisation` of what is included."""
    voice = plan.minutes * utilisation * BLENDED
    wa = plan.whatsapp * utilisation * WA_BLENDED_INR
    nums = plan.numbers * NUMBER_INR
    cost = voice + wa + nums + plan.price * RAZORPAY_FEE
    profit = plan.price - cost
    return cost, profit, profit / plan.price


def free_tier_worst_case(minutes: int = 10, wa: int = 50) -> dict[str, float]:
    return {
        "voice_all_outbound": minutes * OUTBOUND,
        "voice_all_inbound": minutes * INBOUND,
        "whatsapp_all_marketing": wa * WA_MARKETING_INR,
        "number_first_month": NUMBER_INR,
    }


# --- Channel packs (docs/CHANNEL_PACKS.md) ---------------------------------
# Email and SMS costs. Email through an SES-class API is $0.10 per 1,000; Resend's overage is
# $0.90 per 1,000 and is the worst case. India SMS on a DLT route is about ₹0.11 to ₹0.35 a
# segment (published 2026 rate cards).
EMAIL_INR_EXPECTED = 0.10 / 1000 * USD_INR
EMAIL_INR_WORST = 0.90 / 1000 * USD_INR
SMS_INR_EXPECTED = 0.20
SMS_INR_WORST = 0.35
PACK_NUMBERS = 2  # a Call pack unlocks two phone numbers


@dataclass(frozen=True)
class Pack:
    name: str
    price: float  # per 30 days, ex-GST
    minutes: int = 0
    whatsapp: int = 0
    emails: int = 0
    sms: int = 0
    numbers: int = 0


def pack_cost(
    pack: Pack, use: float, *, worst: bool = False, inbound_share: float = INBOUND_SHARE
) -> float:
    """What serving this pack costs in a 30 day window when `use` of its allowance is used."""
    minute = INBOUND * inbound_share + OUTBOUND * (1 - inbound_share)
    wa = WA_MARKETING_INR if worst else WA_BLENDED_INR
    email = EMAIL_INR_WORST if worst else EMAIL_INR_EXPECTED
    sms = SMS_INR_WORST if worst else SMS_INR_EXPECTED
    variable = pack.minutes * minute + pack.whatsapp * wa + pack.emails * email + pack.sms * sms
    return variable * use + pack.numbers * NUMBER_INR + pack.price * RAZORPAY_FEE


def pack_margin(pack: Pack, use: float, **kw) -> float:
    return (pack.price - pack_cost(pack, use, **kw)) / pack.price


CURRENT_PACKS = [
    Pack("Call", 2499, minutes=250, numbers=PACK_NUMBERS),
    Pack("WhatsApp", 1499, whatsapp=1000),
    Pack("Email + SMS", 999, emails=3000, sms=500),
]
PROPOSED_PACKS = [
    Pack("Call", 2199, minutes=250, numbers=PACK_NUMBERS),
    Pack("WhatsApp", 899, whatsapp=1000),
    Pack("Email + SMS", 449, emails=3000, sms=500),
]


def print_packs(title: str, packs: list[Pack]) -> None:
    print(title)
    uses = "".join(f"{int(u * 100):>4d}% use " for u in (1.0, 0.6, 0.4))
    print(f"  {'pack':12s}{'price':>8s}{'cost@100%':>11s}  {uses}   worst case")
    for p in packs:
        cols = "".join(f"{pack_margin(p, u):>9.0%} " for u in (1.0, 0.6, 0.4))
        worst = pack_margin(p, 1.0, worst=True, inbound_share=0.0 if p.minutes else INBOUND_SHARE)
        print(f"  {p.name:12s}{p.price:8,.0f}{pack_cost(p, 1.0):11,.0f}  {cols}   {worst:>6.0%}")
    total = sum(p.price for p in packs)
    print(f"  all three: {total:,.0f} list, {total * 0.9:,.0f} with the 10% bundle discount "
          f"(${total * 0.9 / USD_INR:,.2f})\n")


def main() -> None:
    print("COST OF ONE AI CALL MINUTE (INR)")
    print(f"  inbound   {INBOUND:5.2f}")
    print(f"  outbound  {OUTBOUND:5.2f}   (carrier is {TWILIO_OUT_MOBILE_USD * USD_INR:.2f} of it)")
    print(f"  blended   {BLENDED:5.2f}   ({INBOUND_SHARE:.0%} inbound)")
    print(f"  number rental {NUMBER_INR:.0f}/month each; infra {INFRA_INR_MONTH:,.0f}/month fixed\n")

    print("FREE TIER WORST CASE PER SIGN-UP (INR)")
    worst = free_tier_worst_case()
    for k, v in worst.items():
        print(f"  {k:24s}{v:8.1f}")
    print(f"  {'total, voice outbound':24s}{worst['voice_all_outbound'] + worst['whatsapp_all_marketing'] + worst['number_first_month']:8.1f}\n")

    print("CURRENT LANDING PRICES (Growth 14,999 incl. 3,500 min; overage 3.80)")
    cost = 3500 * BLENDED + 5000 * 0 + 5 * NUMBER_INR + 14999 * RAZORPAY_FEE
    print(f"  cost at full use {cost:,.0f} vs revenue 14,999 -> margin {(14999 - cost) / 14999:.0%}")
    print(f"  overage 3.80/min vs cost {INBOUND:.2f} in / {OUTBOUND:.2f} out -> "
          f"{(3.8 - INBOUND) / 3.8:.0%} in / {(3.8 - OUTBOUND) / 3.8:.0%} out\n")

    print("PROPOSED PLANS: gross margin by how much of the allowance is used")
    print(f"  {'plan':8s}{'price':>9s}{'per min':>9s}  " + "".join(f"{int(u * 100):>5d}% use  " for u in (1.0, 0.6, 0.4)))
    for p in PLANS:
        cols = []
        for u in (1.0, 0.6, 0.4):
            _, _, m = margin(p, u)
            cols.append(f"{m:>8.0%}   ")
        print(f"  {p.name:8s}{p.price:9,.0f}{p.price / p.minutes:9.2f}  " + "".join(cols))
    print()
    for p in PLANS:
        print(f"  {p.name}: overage {p.overage_per_min:.2f}/min -> margin "
              f"{(p.overage_per_min - BLENDED) / p.overage_per_min:.0%} blended, "
              f"{(p.overage_per_min - OUTBOUND) / p.overage_per_min:.0%} if all outbound")
    fixed_need = INFRA_INR_MONTH
    print(f"\nBREAK-EVEN: fixed infra {fixed_need:,.0f}/month is covered by about "
          f"{fixed_need / (PLANS[0].price * (1 - 0.5)):.1f} Starter customers at 50% margin")

    # Cheapest minute we can sell without losing money. Twilio outbound to an Indian
    # mobile is most of the outbound cost, so a call that does not use Twilio (the
    # browser, or the workspace's own Exotel number) drops the carrier and the media
    # surcharge. Sarvam's bundled voice-agent minute is ₹3.50 plus ₹0.40 telephony,
    # which is dearer than buying the speech APIs ourselves.
    speech = STT_INR_PER_MIN + TTS_CHARS_PER_CALL_MIN / 1000 * TTS_INR_PER_1K_CHARS
    llm = TURNS_PER_MIN * (
        LLM_IN_TOKENS_PER_TURN * LLM_IN_INR_PER_M + LLM_OUT_TOKENS_PER_TURN * LLM_OUT_INR_PER_M
    ) / 1_000_000
    own_line = speech + llm
    print("\nCHEAPEST MINUTE (INR), no Twilio")
    print(f"  speech {speech:.2f} + language model {llm:.2f} = {own_line:.2f} to serve")
    print(f"  charge 2.50  (wallet OWN_LINE_PAISE); margin {(2.50 - own_line) / 2.50:.0%} before the payment fee")
    print(f"  Twilio outbound still costs {OUTBOUND:.2f} to serve, so those rates stay at ₹8–9")
    print("  Sarvam voice-agent bundle is 3.50 plus 0.40 telephony = 3.90, dearer than this")


if __name__ == "__main__":
    main()
    print()
    print("CHANNEL PACKS: gross margin by use of the allowance")
    print("  (worst case: every call outbound, every WhatsApp a marketing message,")
    print("   SMS at 0.35, email at overage rates)")
    print_packs("  Current prices", CURRENT_PACKS)
    print_packs("  Proposed prices", PROPOSED_PACKS)
