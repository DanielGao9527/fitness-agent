"""One business calendar for records; provider quotas keep their existing UTC day."""
from datetime import datetime, timedelta, timezone

BUSINESS_TIMEZONE = "Asia/Shanghai"
BEIJING = timezone(timedelta(hours=8))


def business_today():
    return datetime.now(BEIJING).date()
