from app.services.agent_engine import clean_for_speech


def test_strips_reasoning_markdown_and_urls():
    raw = "<think>user wants hours</think>**Sure!** We open at 9. See https://x.co/a for more."
    assert clean_for_speech(raw) == "Sure! We open at 9. See for more."


def test_drops_unterminated_think_block():
    assert clean_for_speech("Hello there. <think>still reasoning") == "Hello there."


def test_flattens_lists():
    assert clean_for_speech("Options:\n- red\n- blue\n1. green") == "Options: red blue green"
