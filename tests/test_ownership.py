from concurrent.futures import ThreadPoolExecutor
from pathlib import PurePosixPath
from threading import Barrier

import pytest

from cammon.importer import RecordingImporter, copy_key
from cammon.ownership import OwnershipConflictError, PathOwnership, folder_key
from tests.test_importer import FIRST, SECOND


def test_preview_does_not_reserve_but_import_reserves_whole_tree(gateway, device, tmp_path):
    folder = gateway.backend.root / "history"
    child = folder / "nested"
    child.mkdir(parents=True)
    source = child / FIRST
    source.write_bytes(b"history")
    importer = RecordingImporter(gateway)
    ownership = PathOwnership(gateway)
    assert importer.scan(device["id"], str(folder))["importable"] == 1
    assert ownership.describe(str(folder))["status"] == "unassigned"
    assert importer.scan(device["id"], str(folder), recursive=False, commit=True)["imported"] == 0
    assert ownership.describe(str(source))["owners"] == [dict(device_id=device["id"], device_name=device["name"])]
    other = gateway.create_device("其他摄像机")
    for path, recursive in ((folder, False), (child, True), (child, False)):
        with pytest.raises(OwnershipConflictError, match=device["name"]):
            importer.scan(other["id"], str(path), recursive=recursive, commit=True)
    assert importer.scan(device["id"], str(child), commit=True)["imported"] == 1
    result = ownership.describe(str(source))
    assert result["recording_ids"] == [1] and result["kind"] == "file"
    source.unlink()
    gateway.db.execute("UPDATE recordings SET state='virtual' WHERE id=1")
    assert ownership.describe(str(source))["status"] == "owned"
    assert not ownership.describe(str(source))["exists"]


def test_sibling_trees_can_have_different_owners_but_parent_cannot_be_claimed(gateway, device):
    folder = gateway.backend.root / "history"
    first, second = folder / "one", folder / "two"
    first.mkdir(parents=True)
    second.mkdir()
    other = gateway.create_device("第二台")
    importer = RecordingImporter(gateway)
    importer.scan(device["id"], str(first), commit=True)
    importer.scan(other["id"], str(second), commit=True)
    result = PathOwnership(gateway).describe(str(folder))
    assert result["status"] == "container"
    assert {row["device_id"] for row in result["owners"]} == {device["id"], other["id"]}
    for recursive in (True, False):
        with pytest.raises(OwnershipConflictError):
            importer.scan(device["id"], str(folder), recursive=recursive, commit=True)
    assert not gateway.get_setting(folder_key("history"))


@pytest.mark.parametrize("association", ["recording", "virtual", "copy"])
def test_legacy_file_associations_also_protect_the_folder(gateway, device, association):
    folder = gateway.backend.root / "history%_"
    folder.mkdir()
    source = folder / FIRST
    source.write_bytes(b"history")
    importer = RecordingImporter(gateway)
    importer.scan(device["id"], str(folder), commit=True)
    gateway.db.execute("DELETE FROM settings WHERE key=?", (folder_key(folder.name),))
    if association == "virtual":
        gateway.db.execute("UPDATE recordings SET state='virtual' WHERE id=1")
        source.unlink()
    if association == "copy":
        gateway.set_setting(copy_key(folder.name + "/" + FIRST), dict(
            device_id=device["id"], recording_id=1, remote_path=folder.name + "/" + FIRST))
        gateway.db.execute("UPDATE recordings SET remote_path=? WHERE id=1",
                           (f"originals/{device['id']}/day/{FIRST}",))
    other = gateway.create_device("其他摄像机")
    # This is a new, previously unindexed source in the same legacy folder.
    (folder / SECOND).write_bytes(b"new history")
    with pytest.raises(OwnershipConflictError):
        importer.scan(other["id"], str(folder), commit=True)
    assert PathOwnership(gateway).describe(str(source))["owners"][0]["device_id"] == device["id"]
    assert gateway.query_files({"device_id": other["id"]})["count"] == 0


def test_historical_mixed_folder_is_reported_and_blocks_both_cameras(gateway, device):
    folder = gateway.backend.root / "mixed"
    folder.mkdir()
    (folder / FIRST).write_bytes(b"history")
    RecordingImporter(gateway).scan(device["id"], str(folder), commit=True)
    gateway.db.execute("DELETE FROM settings WHERE key=?", (folder_key("mixed"),))
    other = gateway.create_device("其他摄像机")
    gateway.set_setting(copy_key("mixed/" + SECOND), dict(
        device_id=other["id"], recording_id=1, remote_path="mixed/" + SECOND))
    result = PathOwnership(gateway).describe(str(folder))
    assert result["status"] == "conflict"
    assert {row["device_id"] for row in result["owners"]} == {device["id"], other["id"]}
    first = PathOwnership(gateway).describe(str(folder / FIRST))
    second = PathOwnership(gateway).describe(str(folder / SECOND))
    assert first["status"] == second["status"] == "owned"
    assert first["owners"][0]["device_id"] == device["id"]
    assert second["owners"][0]["device_id"] == other["id"]
    for camera in (device, other):
        with pytest.raises(OwnershipConflictError):
            RecordingImporter(gateway).scan(camera["id"], str(folder), commit=True)


def test_managed_recordings_and_archive_directories_resolve_camera(gateway, device):
    ownership = PathOwnership(gateway)
    for prefix in ("originals", "archives"):
        result = ownership.describe(str(gateway.backend.root / prefix / device["id"]))
        assert result["status"] == "owned" and result["kind"] == "directory"
        assert result["owners"][0]["device_id"] == device["id"]
    gateway.update_device(device["id"], {"name": "新名称", "enabled": False})
    assert ownership.describe(str(gateway.backend.root / "originals" / device["id"]))["owners"][0][
        "device_name"] == "新名称"


def test_concurrent_different_camera_imports_cannot_share_a_folder(gateway, device):
    folder = gateway.backend.root / "history"
    folder.mkdir()
    (folder / FIRST).write_bytes(b"history")
    other = gateway.create_device("其他摄像机")
    barrier = Barrier(2)

    def run(camera):
        barrier.wait(timeout=5)
        try:
            return RecordingImporter(gateway).scan(camera["id"], str(folder), commit=True)
        except OwnershipConflictError:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        reports = list(pool.map(run, (device, other)))
    assert sum(report is None for report in reports) == 1
    assert sum(report["imported"] for report in reports if report) == 1
    assert gateway.query_files({})["count"] == 1


def test_query_is_read_only_and_rejects_traversal_and_symlinks(gateway, device, tmp_path):
    ownership = PathOwnership(gateway)
    root = gateway.backend.root
    before = gateway.db.metadata_state()["revision"]
    assert ownership.describe(str(root / "unknown"))["status"] == "unassigned"
    assert gateway.db.metadata_state()["revision"] == before
    (root / "link").symlink_to(tmp_path, target_is_directory=True)
    for path in (str(root.parent), str(root / "link" / "file"), str(root) + "/../outside", "relative"):
        with pytest.raises(ValueError):
            ownership.describe(path)


def test_folder_reservation_metadata_includes_camera_and_credentials(gateway, device):
    folder = gateway.backend.root / "history"
    folder.mkdir()
    gateway.db.acknowledge_metadata(gateway.db.metadata_batch(1000))
    RecordingImporter(gateway).scan(device["id"], str(folder), commit=True)
    changes = gateway.db.metadata_batch(1)
    assert {(row["table_name"], tuple(row["key"])) for row in changes} == {
        ("settings", (folder_key("history"),)), ("devices", (device["id"],)),
        ("settings", ("credential:" + device["id"],)),
    }


def test_storage_address_cannot_change_with_only_an_empty_folder_binding(gateway, device, tmp_path):
    gateway.configure_storage({"protocol": "local", "address": str(gateway.backend.root)})
    folder = gateway.backend.root / "empty-history"
    folder.mkdir()
    RecordingImporter(gateway).scan(device["id"], str(folder), commit=True)
    assert gateway.query_files({})["count"] == 0
    with pytest.raises(ValueError, match="已有目录归属"):
        gateway.configure_storage({"protocol": "local", "address": str(tmp_path / "another-disk")})
    assert gateway.backend.root == folder.parent


def test_folder_browse_does_not_rescan_all_recordings_for_each_child(gateway, device, monkeypatch):
    root = gateway.backend.root
    for index in range(40):
        (root / f"history-{index:02}").mkdir()
    with gateway.lock:
        gateway.db.connection.executemany(
            "INSERT INTO recordings(device_id,path,channel,start,end,day,state,created_at,last_write,remote_path) "
            "VALUES (?,?,'00',1,2,'2025-10-08','stored',1,1,?)",
            [(device["id"], f"imports/{index}/video.mp4", f"history-{index % 40:02}/nested/{index}/video.mp4")
             for index in range(1000)],
        )
    comparisons = 0
    original = PurePosixPath.is_relative_to

    def counted(path, other):
        nonlocal comparisons
        comparisons += 1
        return original(path, other)

    monkeypatch.setattr(PurePosixPath, "is_relative_to", counted)
    listing = RecordingImporter(gateway).folders()
    assert len(listing["folders"]) == 40
    assert listing["ownership"]["status"] == "container"
    assert all(child["ownership"]["owners"][0]["device_id"] == device["id"]
               for child in listing["folders"])
    # Bound path comparisons instead of wall time so slow CI hosts can still
    # detect the old folders x recordings scan (over 80,000 comparisons).
    assert comparisons < 2000


def test_browse_index_preserves_path_boundaries_and_removed_subtrees(gateway, device):
    root = gateway.backend.root
    folder = root / "history"
    sibling = root / "history-other"
    folder.mkdir()
    sibling.mkdir()
    RecordingImporter(gateway).scan(device["id"], str(folder), commit=True)
    folder.rmdir()
    ownership = PathOwnership(gateway)
    snapshot = ownership.snapshot()
    assert ownership.describe(str(folder), snapshot)["status"] == "owned"
    assert ownership.describe(str(folder / "missing-child"), snapshot)["status"] == "owned"
    assert ownership.describe(str(sibling), snapshot)["status"] == "unassigned"
    assert ownership.describe(str(root), snapshot)["status"] == "container"
