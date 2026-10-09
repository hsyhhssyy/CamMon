import json

from pydantic import SecretStr

from cammon.database import Database
from cammon.metadata import MetadataSynchronizer
from tests.test_gateway import create


def test_tracking_is_atomic_with_local_transaction(gateway):
    db = gateway.db
    db.acknowledge_metadata(db.metadata_batch(1000))
    before = db.metadata_state()["revision"]
    db.execute("BEGIN IMMEDIATE")
    db.execute("INSERT INTO settings VALUES (?,?)", ("rollback-test", "{}"))
    assert db.metadata_state()["pending"] == 1
    db.execute("ROLLBACK")
    assert db.metadata_state()["revision"] == before
    assert db.metadata_state()["pending"] == 0
    assert gateway.get_setting("rollback-test") is None


def test_coalesced_changes_and_ack_preserve_concurrent_updates(gateway):
    db = gateway.db
    db.acknowledge_metadata(db.metadata_batch(1000))
    for value in range(100):
        gateway.set_setting("policy", {"days": value})
    batch = db.metadata_batch(100)
    assert len(batch) == 1 and json.loads(batch[0]["row"]["value"]) == {"days": 99}
    gateway.set_setting("policy", {"days": 101})
    db.acknowledge_metadata(batch)
    pending = db.metadata_batch(100)
    assert len(pending) == 1 and json.loads(pending[0]["row"]["value"]) == {"days": 101}
    db.acknowledge_metadata(pending)
    assert db.metadata_state()["pending"] == 0


def test_deletes_and_changed_primary_keys_are_tracked(gateway):
    db = gateway.db
    gateway.set_setting("old-key", {"value": 1})
    db.acknowledge_metadata(db.metadata_batch(1000))
    db.execute("UPDATE settings SET key='new-key' WHERE key='old-key'")
    batch = db.metadata_batch(100)
    assert {tuple(c["key"]): c["row"] is None for c in batch} == {("old-key",): True, ("new-key",): False}
    db.acknowledge_metadata(batch)
    db.execute("DELETE FROM settings WHERE key='new-key'")
    assert db.metadata_batch(100)[0]["row"] is None


def test_metadata_and_queue_only_exist_in_memory(gateway):
    db = Database()
    db.execute("INSERT INTO settings VALUES ('existing','{}')")
    batch = db.metadata_batch(100)
    assert len(batch) == 1 and batch[0]["key"] == ["existing"]
    assert db.all("PRAGMA database_list")[0]["file"] == ""
    assert db.one("PRAGMA temp_store")["temp_store"] == 2
    db.close()
    db = Database()
    assert not db.metadata_batch(100)
    db.close()
    assert not (gateway.settings.data_dir / "index.sqlite3").exists()
    assert not (gateway.settings.data_dir / "secret.key").exists()


def test_postgres_errors_are_redacted_and_camera_service_continues(gateway, device, monkeypatch):
    dsn = "postgresql://admin:very-secret-password@invalid:5432/cammon"
    gateway.settings.postgres_dsn = SecretStr(dsn)
    gateway.metadata = MetadataSynchronizer(gateway.db, gateway.settings)

    def fail():
        raise RuntimeError(dsn)

    monkeypatch.setattr(gateway.metadata, "_connect", fail)
    assert not gateway.metadata.sync_once()
    token = create(gateway, device)
    gateway.write(token, b"header", 0)
    assert gateway.read(token, 10, 0) == b"headerdata"
    gateway.close(token)
    gateway.update_device(device["id"], {"retention_days": 7})
    status = gateway.status()["metadata"]
    assert status["configured"] and not status["connected"] and status["pending_rows"] > 0
    assert "RuntimeError" in status["error"]
    assert "very-secret-password" not in str(status) + repr(gateway.settings)
