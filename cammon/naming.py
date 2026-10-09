import errno
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Asia/Shanghai")
VIDEO_NAME = re.compile(r"^([0-9]+)_([0-9]{14})_([0-9]{14})\.mp4$")


def path_parts(path: str) -> list[str]:
    if not isinstance(path, str) or "\x00" in path or "\\" in path or path.startswith("/"):
        raise OSError(errno.EACCES, "Invalid relative path")
    if path in ("", "."):
        return []
    parts = path.split("/")
    if any(p in ("", ".", "..") or ":" in p or len(p.encode()) > 255 for p in parts):
        raise OSError(errno.EACCES, "Invalid path component")
    return parts


def directory_path(path: str, allow_root: bool = False) -> str:
    parts = path_parts(path)
    if len(parts) > 2 or (not parts and not allow_root):
        raise OSError(errno.EACCES, "Only two directory levels are permitted")
    return "/".join(parts)


def recording_path(path: str) -> tuple[str, str, float, float, str]:
    parts = path_parts(path)
    if len(parts) not in (2, 3):
        raise OSError(errno.EACCES, "MP4 files must be in a first- or second-level directory")
    match = VIDEO_NAME.fullmatch(parts[-1])
    if not match:
        raise OSError(errno.EACCES, "Expected X_YYYYMMDDHHmmss_YYYYMMDDHHmmss.mp4")
    channel, start, end = match.groups()
    try:
        begin = datetime.strptime(start, "%Y%m%d%H%M%S").replace(tzinfo=TZ)
        finish = datetime.strptime(end, "%Y%m%d%H%M%S").replace(tzinfo=TZ)
        # strptime accepts some short fields; round-trip also enforces all field widths.
        if begin.strftime("%Y%m%d%H%M%S") != start or finish.strftime("%Y%m%d%H%M%S") != end:
            raise ValueError("Invalid timestamp")
    except ValueError as exc:
        raise OSError(errno.EACCES, "Invalid recording timestamp") from exc
    if finish < begin:
        raise OSError(errno.EACCES, "End timestamp precedes start")
    return "/".join(parts), channel, begin.timestamp(), finish.timestamp(), begin.date().isoformat()


def day_slices(start: float, end: float) -> list[tuple[str, float, float]]:
    """Half-open media intervals; midnight endpoints belong only to the preceding day."""
    slices = []
    cursor = start
    while cursor < end:
        day = datetime.fromtimestamp(cursor, TZ).date()
        boundary = datetime.combine(day + timedelta(days=1), datetime.min.time(), TZ).timestamp()
        stop = min(boundary, end)
        slices.append((day.isoformat(), cursor - start, stop - cursor))
        cursor = stop
    return slices
