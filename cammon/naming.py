import errno
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Asia/Shanghai")
VIDEO_NAME = re.compile(r"^([0-9]+)_([0-9]{14})_([0-9]{14})\.mp4$")
MINUTE_VIDEO_NAME = re.compile(r"^([0-5]?[0-9])M([0-5]?[0-9])S_([0-9]{10})\.mp4$")
HOUR_DIRECTORY = re.compile(r"^[0-9]{10}$")
XIAOMI_DEVICE = re.compile(r"^[0-9a-fA-F]{12}$")


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
    xiaomi_hour = (len(parts) == 3 and parts[0] == "xiaomi_camera_videos"
                   and XIAOMI_DEVICE.fullmatch(parts[1]) and HOUR_DIRECTORY.fullmatch(parts[2]))
    if xiaomi_hour:
        _parse_datetime(parts[2], "%Y%m%d%H")
    elif len(parts) > 2 or (not parts and not allow_root):
        raise OSError(errno.EACCES, "Only two directory levels or xiaomi_camera_videos/MAC/YYYYMMDDHH are permitted")
    return "/".join(parts)


def _parse_datetime(value: str, format: str) -> datetime:
    try:
        parsed = datetime.strptime(value, format).replace(tzinfo=TZ)
        # strptime accepts some short fields; round-trip also enforces all field widths.
        if parsed.strftime(format) != value:
            raise ValueError("Invalid timestamp")
    except ValueError as exc:
        raise OSError(errno.EACCES, "Invalid recording timestamp") from exc
    return parsed


def is_minute_recording(path: str) -> bool:
    return MINUTE_VIDEO_NAME.fullmatch(path.rsplit("/", 1)[-1]) is not None


def recording_name(name: str, hour_directory: str | None = None) -> tuple[str, float, float, str]:
    minute = MINUTE_VIDEO_NAME.fullmatch(name)
    if minute:
        minutes, seconds, stamp = minute.groups()
        start = int(stamp)
        begin = datetime.fromtimestamp(start, TZ)
        if (begin.minute, begin.second) != (int(minutes), int(seconds)):
            raise OSError(errno.EACCES, "Recording minute/second does not match the Unix timestamp")
        if (hour_directory is not None and HOUR_DIRECTORY.fullmatch(hour_directory)
                and begin.strftime("%Y%m%d%H") != hour_directory):
            raise OSError(errno.EACCES, "Recording hour directory does not match the Unix timestamp")
        # The name has no end time. Cached files use a nominal minute until the
        # sealed video is probed; imported videos are probed before adoption.
        return "00", start, start + 60, begin.date().isoformat()
    match = VIDEO_NAME.fullmatch(name)
    if not match:
        raise OSError(errno.EACCES, "Expected X_YYYYMMDDHHmmss_YYYYMMDDHHmmss.mp4 or mmMssS_UNIX.mp4")
    channel, start, end = match.groups()
    begin = _parse_datetime(start, "%Y%m%d%H%M%S")
    finish = _parse_datetime(end, "%Y%m%d%H%M%S")
    if finish < begin:
        raise OSError(errno.EACCES, "End timestamp precedes start")
    return channel, begin.timestamp(), finish.timestamp(), begin.date().isoformat()


def recording_path(path: str) -> tuple[str, str, float, float, str]:
    parts = path_parts(path)
    if len(parts) not in (2, 3, 4):
        raise OSError(errno.EACCES, "MP4 files must be in a permitted recording directory")
    directory_path("/".join(parts[:-1]))
    if len(parts) == 4 and not is_minute_recording(parts[-1]):
        raise OSError(errno.EACCES, "The Xiaomi hour directory requires mmMssS_UNIX.mp4")
    return "/".join(parts), *recording_name(parts[-1], parts[-2])


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
