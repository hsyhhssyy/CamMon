import pytest

from cammon.naming import directory_path, recording_name, recording_path


@pytest.mark.parametrize("minute,stamp", [(30, 1753536633), (31, 1753536693), (32, 1753536753), (33, 1753536813)])
def test_screenshot_minute_names_use_unix_seconds_and_beijing_hour(minute, stamp):
    path = f"xiaomi_camera_videos/788b2a487711/2025072621/{minute}M33S_{stamp}.mp4"
    assert recording_path(path) == (path, "00", stamp, stamp + 60, "2025-07-26")


def test_minute_name_can_be_parsed_without_original_directory():
    assert recording_name("30M33S_1753536633.mp4") == ("00", 1753536633, 1753536693, "2025-07-26")
    assert recording_path("imports/abcdef1234567890/0M0S_1753534800.mp4")[1:] == (
        "00", 1753534800, 1753534860, "2025-07-26",
    )


@pytest.mark.parametrize("path", [
    "30M33S_1753536633.mp4", "day/60M33S_1753536633.mp4", "day/30M60S_1753536633.mp4",
    "day/31M33S_1753536633.mp4", "day/30M32S_1753536633.mp4", "day/30M33S_1753536633000.mp4",
    "day/30M33S_175353663.mp4", "day/30M33S_invalid.mp4", "day/30M33S_1753536633.mp4:stream",
    "2025072613/30M33S_1753536633.mp4", "day/hour/minute/30M33S_1753536633.mp4",
    "xiaomi_camera_videos/not-a-mac/2025072621/30M33S_1753536633.mp4",
    "xiaomi_camera_videos/788b2a487711/2025072620/30M33S_1753536633.mp4",
    "xiaomi_camera_videos/788b2a487711/2025072621/deeper/30M33S_1753536633.mp4",
    "xiaomi_camera_videos/788b2a487711/2025072621/00_20250726213033_20250726213133.mp4",
])
def test_invalid_minute_names_and_paths_are_rejected(path):
    with pytest.raises(OSError):
        recording_path(path)


@pytest.mark.parametrize("path", [
    "day/hour/minute", "xiaomi_camera_videos/788b2a487711/2025023021",
    "xiaomi_camera_videos/788b2a487711/2025072624", "xiaomi_camera_videos/788b2a487711/2025072621/deeper",
])
def test_third_directory_level_is_reserved_for_valid_xiaomi_hour_folders(path):
    with pytest.raises(OSError):
        directory_path(path)
