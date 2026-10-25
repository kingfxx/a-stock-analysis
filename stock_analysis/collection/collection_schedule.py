"""Calendar slots shared by registered collectors (Asia/Shanghai)."""
from datetime import timedelta
from zoneinfo import ZoneInfo

ZONE = ZoneInfo('Asia/Shanghai')


def latest_daily_date(now, local_time):
    local = now.astimezone(ZONE)
    return local.date() - timedelta(days=int(local.strftime('%H:%M') < local_time))
