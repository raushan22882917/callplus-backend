import pytest
from app.services.email_ai import parse_email_draft
from app.services.mailbox import MailboxError, assert_mail_host, build_message


def test_mail_host_allows_public_names_and_blocks_local_addresses() -> None:
    assert assert_mail_host(" SMTP.Gmail.com. ") == "smtp.gmail.com"
    for blocked in ("localhost", "127.0.0.1", "10.0.0.5", "169.254.169.254", "metadata.google.internal"):
        with pytest.raises(MailboxError):
            assert_mail_host(blocked)


def test_email_draft_parser_keeps_placeholders() -> None:
    drafted = parse_email_draft('Sure.\n{"subject": "Hello {{first_name}}", "body": "Hi {{name}}"}')
    assert drafted == {"subject": "Hello {{first_name}}", "body": "Hi {{name}}"}
    assert parse_email_draft("not json") is None


def test_outbound_message_keeps_reply_headers() -> None:
    message = build_message(
        sender="Shop <hello@shop.test>",
        to_email="priya@example.com",
        subject="Hello",
        text="Hi",
        html="<p>Hi</p>",
        message_id="<abc@shop.test>",
        reply_to="replies@shop.test",
        in_reply_to="<old@shop.test>",
        references=["<old@shop.test>"],
    )
    assert message["From"] == "Shop <hello@shop.test>"
    assert message["Message-ID"] == "<abc@shop.test>"
    assert message["Reply-To"] == "replies@shop.test"
    assert "<old@shop.test>" in message["References"]
