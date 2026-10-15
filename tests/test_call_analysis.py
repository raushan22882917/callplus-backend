from datetime import UTC, datetime

from app.services.agent_engine import history_note
from app.services.call_analysis import parse_analysis


def test_parse_analysis_clamps_values_and_strips_reasoning():
    text = (
        '<think>hmm</think>```json\n{"summary": "Wants a haircut tomorrow at 3.", "sentiment": "HAPPY",'
        ' "intent": "Book haircut", "outcome": "callback_requested", "action_items": ["Call to confirm", ""]}\n```'
    )
    result = parse_analysis(text)
    assert result == {
        "summary": "Wants a haircut tomorrow at 3.",
        "sentiment": "neutral",
        "intent": "Book haircut",
        "outcome": "callback_requested",
        "action_items": ["Call to confirm"],
    }


def test_parse_analysis_rejects_non_json():
    assert parse_analysis("sorry, I cannot") is None
    assert parse_analysis("[1, 2]") is None


def test_history_note_empty_and_filled():
    assert history_note([], []) == ""
    note = history_note([(datetime(2026, 10, 1, tzinfo=UTC), "Asked about order status.")], ["Refund request"])
    assert "01 Oct 2026" in note and "Asked about order status." in note and "Refund request" in note
