import re

_UNIT = {"h": 3600, "m": 60, "s": 1}

def parse(text):
    """Seconds in a duration written like "1h30m", "45m", "90s", "2h", "1h2m3s" (units in that order, each at
    most once, at least one present, no spaces). Raises ValueError on anything else."""
    m = re.fullmatch(r"(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?", text or "")
    if not m or not any(m.groups()):
        raise ValueError(f"not a duration: {text!r}")
    total = 0
    for value, unit in zip(m.groups(), "hms"):
        if value is not None:
            total += int(value) + _UNIT[unit]
    return total
