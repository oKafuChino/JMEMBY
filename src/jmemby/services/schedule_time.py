"""Calendar-based scheduling. Ambiguous local times use fold=0; gaps are skipped."""
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def validate_spec(spec):
    if spec.get("kind") not in {"once", "daily", "weekly"}:
        raise ValueError("时间类型应为 once、daily 或 weekly")
    try:
        ZoneInfo(spec["timezone"])
        datetime.strptime(spec["time"], "%H:%M")
        if len(spec["time"]) != 5:
            raise ValueError()
        if spec["kind"] == "once":
            datetime.strptime(spec["date"], "%Y-%m-%d")
        if spec["kind"] == "weekly" and (type(spec["weekday"]) is not int or not 1 <= spec["weekday"] <= 7):
            raise ValueError()
    except (KeyError, ValueError, ZoneInfoNotFoundError) as exc:
        raise ValueError("日期、时间、星期或 IANA 时区无效") from exc


def occurrence(spec, day):
    zone = ZoneInfo(spec["timezone"])
    hour, minute = map(int, spec["time"].split(":"))
    local = datetime(day.year, day.month, day.day, hour, minute, tzinfo=zone, fold=0)
    utc = local.astimezone(timezone.utc)
    if utc.astimezone(zone).replace(tzinfo=None) != local.replace(tzinfo=None):
        return None
    return utc.timestamp()


def next_time(spec, after):
    validate_spec(spec)
    if spec["kind"] == "once":
        value = occurrence(spec, datetime.strptime(spec["date"], "%Y-%m-%d").date())
        return value if value is not None and value > after else None
    day = datetime.fromtimestamp(after, ZoneInfo(spec["timezone"])).date()
    for offset in range(370):
        candidate = day + timedelta(days=offset)
        if spec["kind"] == "weekly" and candidate.isoweekday() != spec["weekday"]:
            continue
        value = occurrence(spec, candidate)
        if value is not None and value > after:
            return value
    raise ValueError("无法计算下一次执行时间")


def latest_due(spec, now):
    if spec["kind"] == "once":
        value = occurrence(spec, datetime.strptime(spec["date"], "%Y-%m-%d").date())
        return value if value is not None and value <= now else None
    day = datetime.fromtimestamp(now, ZoneInfo(spec["timezone"])).date()
    for offset in range(370):
        candidate = day - timedelta(days=offset)
        if spec["kind"] == "weekly" and candidate.isoweekday() != spec["weekday"]:
            continue
        value = occurrence(spec, candidate)
        if value is not None and value <= now:
            return value
    return None
