import pytest
from app.services.call_closing import caller_is_closing, conversation_is_over, reply_is_farewell


@pytest.mark.parametrize(
    "text",
    [
        "Bye bye.",
        "bye",
        "Nothing nothing.",
        "No, that's all",
        "Thank you, bye",
        "Nothing else, thanks",
        "नहीं बस",
        "धन्यवाद",
        "Okay thanks bye bye",
        "That's all.",
    ],
)
def test_caller_closing_lines(text: str) -> None:
    assert caller_is_closing(text)


@pytest.mark.parametrize(
    "text",
    [
        "",
        "Okay",
        "Good",
        "Yes",
        "Haircut",
        "Today at 3 p.m.",
        "No I want it tomorrow instead please",
        "Thank you can you also check my last booking",
        "I want to book an appointment",
    ],
)
def test_caller_not_closing(text: str) -> None:
    assert not caller_is_closing(text)


@pytest.mark.parametrize(
    "reply",
    [
        "Okay, take care. Bye.",
        "Bye, have a good day.",
        "Thanks for calling. Goodbye!",
        "ठीक है, अलविदा।",
    ],
)
def test_farewell_replies(reply: str) -> None:
    assert reply_is_farewell(reply)


@pytest.mark.parametrize(
    "reply",
    [
        "",
        "Which day would you like?",
        "Bye? Or should I also check anything else for you today?",
        "Your haircut is noted for three pm and someone will call you back to confirm it, "
        "so please keep your phone nearby today and goodbye",
    ],
)
def test_not_farewell_replies(reply: str) -> None:
    assert not reply_is_farewell(reply)


def test_hangs_up_only_when_both_sides_are_closing() -> None:
    assert conversation_is_over(transcript="Nothing nothing.", reply="Okay, take care. Bye.")
    # caller still has business: a stray "bye" from the model must not cut them off
    assert not conversation_is_over(transcript="Haircut", reply="Okay, bye.")
    # caller said no mid-booking and the agent is still working
    assert not conversation_is_over(transcript="No", reply="Which number should we use?")


@pytest.mark.parametrize(
    "text",
    [
        "Disconnect the call please.",
        "please disconnect the call",
        "Hang up",
        "hang up now",
        "end the call",
        "Can you cut the call?",
        "just disconnect",
        "disconnect kar do",
        "call cut kar do",
        "फोन रख दो",
        "कॉल कट कर दो",
    ],
)
def test_caller_asks_to_end_the_call(text: str) -> None:
    from app.services.call_closing import caller_wants_to_end

    assert caller_wants_to_end(text)
    # obeyed even if the agent's reply isn't a tidy goodbye
    assert conversation_is_over(transcript=text, reply="Sure, one moment.")


@pytest.mark.parametrize(
    "text",
    [
        "Don't hang up",
        "please do not disconnect the call",
        "I was disconnected earlier",
        "my phone keeps disconnecting",
        "what happens when I end the month",
        "Haircut",
        "I want to book an appointment for tomorrow",
        "",
        "I called yesterday and the call was cut but I would like to book a haircut for Saturday afternoon",
    ],
)
def test_other_talk_is_not_a_request_to_end(text: str) -> None:
    from app.services.call_closing import caller_wants_to_end

    assert not caller_wants_to_end(text)
    assert not conversation_is_over(transcript=text, reply="Sure, which day?")
