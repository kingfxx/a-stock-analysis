"""Calendar slots shared by registered collectors (Asia/Shanghai)."""
from datetime import date, timedelta
from zoneinfo import ZoneInfo

ZONE = ZoneInfo('Asia/Shanghai')


def latest_daily_date(now, local_time):
    local = now.astimezone(ZONE)
    return local.date() - timedelta(days=int(local.strftime('%H:%M') < local_time))


def latest_financial_season_date(day, days_before, days_after):
    """Latest daily slot in the annual deadline windows, including missed slots."""
    candidates = []
    for year in range(day.year - 1, day.year + 2):
        for month, last in ((4, 30), (8, 31), (10, 31)):
            deadline = date(year, month, last)
            start = deadline - timedelta(days=days_before)
            end = deadline + timedelta(days=days_after)
            if start <= day:
                candidates.append(min(day, end))
    return max(candidates)
