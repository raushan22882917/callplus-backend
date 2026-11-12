from datetime import date

from app.services.whatsapp_studio import fill_series


def test_fill_series_keeps_sent_and_replies():
    today = date(2026, 11, 11)
    rows = fill_series({today: 3}, {date(2026, 11, 10): 1}, today, days=3)
    assert [(row["day"], row["sent"], row["replies"]) for row in rows] == [
        (date(2026, 11, 9), 0, 0),
        (date(2026, 11, 10), 0, 1),
        (date(2026, 11, 11), 3, 0),
    ]
