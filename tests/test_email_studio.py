from datetime import date

from app.services.email_studio import fill_days


def test_fill_days_keeps_every_day_and_the_counts():
    today = date(2026, 11, 11)
    rows = fill_days({today: 4, date(2026, 11, 9): 2}, today, days=3)
    assert [(row["day"], row["count"]) for row in rows] == [
        (date(2026, 11, 9), 2),
        (date(2026, 11, 10), 0),
        (date(2026, 11, 11), 4),
    ]
