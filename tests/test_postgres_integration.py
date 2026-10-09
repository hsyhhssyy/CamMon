"""Real PostgreSQL outage, crash/restart and replay tests; no mocked DB driver."""

import json
import os
import pwd
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import uuid
from pathlib import Path

import psycopg
import pytest
from psycopg import sql
from pydantic import SecretStr

from cammon.core import Gateway
from cammon.metadata import MetadataBootstrapError, MetadataSynchronizer
from cammon.storage import LocalBackend
from tests.test_gateway import FIRST, create

pytestmark = [pytest.mark.integration, pytest.mark.postgres]


class Postgres:
    def __init__(self, base, binaries, prefix, port):
        self.base, self.binaries, self.prefix, self.port = base, binaries, prefix, port
        self.running = False
        self.dsn = f"postgresql://cammon@127.0.0.1:{port}/postgres"

    def command(self, *args):
        result = subprocess.run([*self.prefix, *map(str, args)], capture_output=True, text=True, timeout=30)
        assert result.returncode == 0, result.stdout + result.stderr

    def start(self):
        self.command(self.binaries / "pg_ctl", "-D", self.base / "data", "-l", self.base / "server.log",
                     "-o", f"-h 127.0.0.1 -p {self.port} -k {self.base}", "-w", "-t", "10", "start")
        self.running = True

    def stop(self):
        if self.running:
            self.command(self.binaries / "pg_ctl", "-D", self.base / "data", "-w", "-t", "10",
                         "-m", "immediate", "stop")
            self.running = False


@pytest.fixture(scope="module")
def postgres():
    candidates = sorted(Path("/usr/lib/postgresql").glob("*/bin/initdb"),
                        key=lambda p: int(p.parent.parent.name))
    if not os.environ.get("CAMMON_TEST_POSTGRES") or not candidates:
        pytest.skip("Set CAMMON_TEST_POSTGRES=1 with PostgreSQL server binaries installed")
    base = Path(tempfile.mkdtemp(prefix="cammon-postgres-"))
    prefix = []
    if os.geteuid() == 0:
        owner = pwd.getpwnam("postgres")
        os.chown(base, owner.pw_uid, owner.pw_gid)
        prefix = ["runuser", "-u", "postgres", "--"]
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    server = Postgres(base, candidates[-1].parent, prefix, port)
    try:
        server.command(server.binaries / "initdb", "-D", base / "data", "-U", "cammon", "-A", "trust",
                       "--no-locale", "--encoding=UTF8")
        server.start()
        yield server
    finally:
        server.stop()
        shutil.rmtree(base)


@pytest.fixture
def pg_gateway(gateway, postgres):
    gateway.settings.postgres_dsn = SecretStr(postgres.dsn)
    gateway.settings.postgres_schema = "test_" + uuid.uuid4().hex
    gateway.settings.metadata_sync_seconds = 0.1
    gateway.metadata = MetadataSynchronizer(gateway.db, gateway.settings)
    yield gateway
    gateway.metadata.stop()
    if not postgres.running:
        postgres.start()
    with psycopg.connect(postgres.dsn, autocommit=True) as conn:
        conn.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(
            sql.Identifier(gateway.settings.postgres_schema)))


def drain(g):
    for _ in range(100):
        assert g.metadata.sync_once(), g.metadata.error
        if not g.db.metadata_state()["pending"]:
            return
    pytest.fail("Metadata backlog did not drain")


def remote_row(g, table, column, key):
    with psycopg.connect(g.settings.postgres_dsn.get_secret_value()) as conn:
        conn.row_factory = psycopg.rows.dict_row
        return conn.execute(sql.SQL("SELECT * FROM {} WHERE {}=%s").format(
            sql.Identifier(g.settings.postgres_schema, table), sql.Identifier(column)), (key,)).fetchone()


def test_migrates_existing_metadata_and_preserves_all_tables(pg_gateway):
    g = pg_gateway
    device = g.create_device("前门", 14)
    g.mkdir(device["id"], "day")
    token = create(g, device)
    g.close(token)
    g.db.execute("INSERT INTO archive_jobs(id,device_id,channel,day,sources,created_at,updated_at) "
                 "VALUES (?,?,?,?,?,?,?)", ("job", device["id"], "00", "2025-10-08",
                                            '[{"id":1,"offset":0,"length":60}]', 1, 1))
    g.db.execute("INSERT INTO coverage VALUES (1,'2025-10-08','job')")
    g.db.execute("INSERT INTO sessions VALUES ('hashed-token',1234)")
    g.settings.metadata_batch_size = 2
    drain(g)
    assert remote_row(g, "devices", "id", device["id"])["retention_days"] == 14
    assert remote_row(g, "recordings", "id", 1)["channel"] == "00"
    assert remote_row(g, "recordings", "id", 1)["size"] == 10
    assert remote_row(g, "directories", "id", 1)["path"] == "day"
    assert remote_row(g, "coverage", "recording_id", 1)["job_id"] == "job"
    assert json.loads(remote_row(g, "archive_jobs", "id", "job")["sources"]) == [{"id": 1, "offset": 0, "length": 60}]
    assert remote_row(g, "sessions", "token_hash", "hashed-token")["expires"] == 1234
    assert "password_hash" in json.loads(remote_row(g, "settings", "key", "admin")["value"])
    assert g.metadata.status()["connected"] and g.metadata.status()["pending_rows"] == 0


def test_outage_keeps_memory_config_writes_and_flush_then_resynchronizes(pg_gateway, postgres, tmp_path, monkeypatch):
    g = pg_gateway
    monkeypatch.setattr("cammon.core.make_backend", lambda *args: LocalBackend(tmp_path / "remote"))
    g.configure_storage({"protocol": "smb", "address": "smb://example/recordings", "username": "writer",
                         "password": "old-backend-password"})
    device = g.create_device("门口", 30)
    g.mkdir(device["id"], "day")
    drain(g)
    postgres.stop()
    assert not g.metadata.sync_once()
    g.update_device(device["id"], {"retention_days": 7})
    g.configure_storage({"protocol": "smb", "address": "smb://example/recordings", "username": "writer",
                         "password": "new-backend-password"})
    token = create(g, device)
    g.write(token, b"header", 0)
    g.close(token)
    # Video backend is independent: completed files can still be published.
    assert g.seal_candidates(time.time() + 1900) == [1]
    assert g.flush_one(1)
    g.set_setting("offline-policy", {"days": 7})
    token = g.open(device["id"], FIRST, os.O_RDONLY)
    assert g.read(token, 10, 0) == b"headerdata"
    g.close(token)
    postgres.start()
    drain(g)
    assert remote_row(g, "devices", "id", device["id"])["retention_days"] == 7
    assert remote_row(g, "recordings", "id", 1)["state"] == "stored"
    assert remote_row(g, "recordings", "id", 1)["sha256"]
    assert json.loads(remote_row(g, "settings", "key", "offline-policy")["value"]) == {"days": 7}
    assert "new-backend-password" not in remote_row(g, "settings", "key", "storage")["value"]


def test_cold_boot_loads_postgres_and_discards_unsynced_memory_and_cache(pg_gateway, postgres, tmp_path, monkeypatch):
    g = pg_gateway
    monkeypatch.setattr("cammon.core.make_backend", lambda *args: LocalBackend(tmp_path / "remote"))
    g.configure_storage({"protocol": "smb", "address": "smb://example/recordings", "username": "writer",
                         "password": "saved-backend-password"})
    device = g.create_device("门口", 14)
    g.mkdir(device["id"], "day")
    token = create(g, device)
    g.close(token)
    g.seal_candidates(time.time() + 1900)
    assert g.flush_one(1)
    temporary = "day/00_20251008090000_20251008090100.mp4"
    token = create(g, device, temporary)
    g.close(token)
    drain(g)
    settings = g.settings
    settings.admin_password = ""  # The saved PostgreSQL admin takes precedence.
    postgres.stop()
    assert not g.metadata.sync_once()
    g.update_device(device["id"], {"retention_days": 3})
    g.set_setting("unsynced", {"will": "be lost"})
    token = create(g, device, "day/00_20251008100000_20251008100100.mp4")
    g.close(token)
    g.shutdown()
    with pytest.raises(MetadataBootstrapError, match="启动时需要"):
        Gateway(settings)
    postgres.start()
    # No local metadata or credentials survive; rehydrate from PostgreSQL.
    shutil.rmtree(settings.data_dir / "shares")
    recovered = Gateway(settings)
    try:
        assert recovered.device(device["id"])["retention_days"] == 14
        assert recovered.get_setting("unsynced") is None
        assert recovered.storage_config(reveal=True)["password"] == "saved-backend-password"
        assert recovered.device_password(device["id"]) == device["password"]
        assert recovered.query_files({})["count"] == 1
        assert recovered.db.one("SELECT state FROM recordings WHERE id=2")["state"] == "deleted"
        assert not list(recovered.blobs.iterdir())
        assert not (settings.data_dir / "index.sqlite3").exists()
        assert not (settings.data_dir / "secret.key").exists()
        token = recovered.open(device["id"], FIRST, os.O_RDONLY)
        assert recovered.read(token, 10, 0) == b"video-data"
        recovered.close(token)
        drain(recovered)
        assert remote_row(recovered, "recordings", "id", 2)["state"] == "deleted"
    finally:
        recovered.shutdown()


def test_lost_commit_acknowledgement_replays_without_duplicate_rows(pg_gateway, monkeypatch):
    g = pg_gateway
    device = g.create_device("入口")
    acknowledge = g.db.acknowledge_metadata

    def lost_ack(*args):
        raise OSError("Crash after PostgreSQL commit")

    monkeypatch.setattr(g.db, "acknowledge_metadata", lost_ack)
    assert not g.metadata.sync_once()
    assert remote_row(g, "devices", "id", device["id"])["name"] == "入口"
    assert g.db.metadata_state()["pending"] > 0
    monkeypatch.setattr(g.db, "acknowledge_metadata", acknowledge)
    drain(g)
    with psycopg.connect(g.settings.postgres_dsn.get_secret_value()) as conn:
        assert conn.execute(sql.SQL("SELECT count(*) FROM {}").format(
            sql.Identifier(g.settings.postgres_schema, "devices"))).fetchone()[0] == 1


def test_pg_io_does_not_hold_camera_lock_and_newer_changes_remain_pending(pg_gateway, monkeypatch):
    g = pg_gateway
    device = g.create_device("入口")
    g.mkdir(device["id"], "day")
    drain(g)
    entered, camera_done = threading.Event(), threading.Event()
    apply = g.metadata._apply
    result = []

    def delayed(conn, changes):
        entered.set()
        apply(conn, changes)

    def camera():
        token = create(g, device)
        g.close(token)
        g.update_device(device["id"], {"retention_days": 3})
        camera_done.set()

    g.update_device(device["id"], {"retention_days": 10})
    monkeypatch.setattr(g.metadata, "_apply", delayed)
    sync = threading.Thread(target=lambda: result.append(g.metadata.sync_once()))
    writer = threading.Thread(target=camera)
    with psycopg.connect(g.settings.postgres_dsn.get_secret_value()) as blocker:
        blocker.execute(sql.SQL("LOCK TABLE {} IN ACCESS EXCLUSIVE MODE").format(g.metadata.table("devices")))
        sync.start()
        assert entered.wait(5)
        writer.start()
        try:
            assert camera_done.wait(2), "Camera blocked behind PostgreSQL network I/O"
        finally:
            blocker.rollback()
            writer.join(5)
            sync.join(5)
    assert result == [True]
    assert remote_row(g, "devices", "id", device["id"])["retention_days"] == 10
    assert g.metadata.status()["pending_rows"] > 0
    monkeypatch.setattr(g.metadata, "_apply", apply)
    drain(g)
    assert remote_row(g, "devices", "id", device["id"])["retention_days"] == 3


def test_physical_deletion_is_replayed(pg_gateway):
    g = pg_gateway
    g.set_setting("temporary", {"value": 1})
    drain(g)
    g.db.execute("DELETE FROM settings WHERE key='temporary'")
    drain(g)
    assert remote_row(g, "settings", "key", "temporary") is None


def test_failed_postgres_transaction_keeps_entire_batch_pending(pg_gateway, monkeypatch):
    g = pg_gateway
    device = g.create_device("入口")
    drain(g)
    g.update_device(device["id"], {"retention_days": 7})
    g.set_setting("new-policy", {"days": 7})
    apply = g.metadata._apply

    def fail_mid_transaction(conn, changes):
        apply(conn, changes)
        conn.execute("SELECT 1/0")

    monkeypatch.setattr(g.metadata, "_apply", fail_mid_transaction)
    assert not g.metadata.sync_once()
    assert "22012" in g.metadata.error
    assert g.db.metadata_state()["pending"] == 2
    assert remote_row(g, "devices", "id", device["id"])["retention_days"] == 30
    assert remote_row(g, "settings", "key", "new-policy") is None
    monkeypatch.setattr(g.metadata, "_apply", apply)
    drain(g)
    assert remote_row(g, "devices", "id", device["id"])["retention_days"] == 7


def test_schema_ownership_and_concurrent_instance_are_rejected(pg_gateway, tmp_path):
    g = pg_gateway
    drain(g)
    settings = g.settings.model_copy(update={"data_dir": tmp_path / "other"})
    with pytest.raises(MetadataBootstrapError, match="另一个"):
        Gateway(settings)
    g.metadata.stop()
    other = Gateway(settings)
    try:
        assert other.get_setting("admin") == g.get_setting("admin")
        assert other.db.metadata_state()["gateway_id"] == g.db.metadata_state()["gateway_id"]
        assert remote_row(g, "settings", "key", "admin")["value"] == g.db.one(
            "SELECT value FROM settings WHERE key='admin'")["value"]
    finally:
        other.shutdown()


def test_unowned_existing_tables_are_never_overwritten(pg_gateway):
    g = pg_gateway
    with psycopg.connect(g.settings.postgres_dsn.get_secret_value(), autocommit=True) as conn:
        conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(g.settings.postgres_schema)))
        conn.execute(sql.SQL("CREATE TABLE {}(id TEXT PRIMARY KEY)").format(g.metadata.table("devices")))
        conn.execute(sql.SQL("INSERT INTO {} VALUES('keep-me')").format(g.metadata.table("devices")))
    assert not g.metadata.sync_once()
    assert "同名表" in g.metadata.error
    assert remote_row(g, "devices", "id", "keep-me") == {"id": "keep-me"}


def test_stale_local_copy_cannot_overwrite_newer_postgres(pg_gateway):
    g = pg_gateway
    drain(g)
    g.metadata._disconnect()
    with psycopg.connect(g.settings.postgres_dsn.get_secret_value(), autocommit=True) as conn:
        conn.execute(sql.SQL("UPDATE {} SET local_revision=local_revision+100").format(
            g.metadata.table("_cammon_registry")))
    assert not g.metadata.sync_once()
    assert "比运行中的内存副本更新" in g.metadata.error


def test_precreated_schema_does_not_require_database_create_privilege(pg_gateway):
    g = pg_gateway
    role = "restricted_" + uuid.uuid4().hex
    with psycopg.connect(g.settings.postgres_dsn.get_secret_value(), autocommit=True) as conn:
        conn.execute(sql.SQL("CREATE ROLE {} LOGIN").format(sql.Identifier(role)))
        conn.execute(sql.SQL("CREATE SCHEMA {} AUTHORIZATION {}").format(
            sql.Identifier(g.settings.postgres_schema), sql.Identifier(role)))
    g.settings.postgres_dsn = SecretStr(g.settings.postgres_dsn.get_secret_value().replace("cammon@", role + "@"))
    g.metadata = MetadataSynchronizer(g.db, g.settings)
    try:
        drain(g)
        assert g.metadata.status()["connected"]
    finally:
        g.metadata.stop()
        # Drop owned objects before dropping the isolated test role.
        with psycopg.connect(g.settings.postgres_dsn.get_secret_value().replace(role + "@", "cammon@"),
                             autocommit=True) as conn:
            conn.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(g.settings.postgres_schema)))
            conn.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(role)))
        g.settings.postgres_dsn = SecretStr(g.settings.postgres_dsn.get_secret_value().replace(role + "@", "cammon@"))


def test_remote_restore_rebuilds_complete_snapshot(pg_gateway):
    g = pg_gateway
    device = g.create_device("入口")
    g.set_setting("removed", {})
    drain(g)
    g.db.execute("DELETE FROM settings WHERE key='removed'")
    drain(g)
    g.metadata._disconnect()
    # Simulate a PostgreSQL restore to an earlier revision, including a stale row.
    with psycopg.connect(g.settings.postgres_dsn.get_secret_value(), autocommit=True) as conn:
        conn.execute(sql.SQL("UPDATE {} SET local_revision=0").format(g.metadata.table("_cammon_registry")))
        conn.execute(sql.SQL("DELETE FROM {}").format(g.metadata.table("devices")))
        conn.execute(sql.SQL("INSERT INTO {} VALUES('removed','{{}}',0)").format(g.metadata.table("settings")))
    drain(g)
    assert remote_row(g, "devices", "id", device["id"])["name"] == "入口"
    assert remote_row(g, "settings", "key", "removed") is None


def test_restore_between_snapshot_batches_with_same_revision_is_detected(pg_gateway):
    g = pg_gateway
    device = g.create_device("入口")
    g.mkdir(device["id"], "day")
    g.settings.metadata_batch_size = 1
    assert g.metadata.sync_once()
    early = remote_row(g, "_cammon_registry", "id", 1)
    drain(g)
    complete = remote_row(g, "_cammon_registry", "id", 1)
    assert early["local_revision"] == complete["local_revision"]
    assert early["sync_sequence"] < complete["sync_sequence"]
    g.metadata._disconnect()
    with psycopg.connect(g.settings.postgres_dsn.get_secret_value(), autocommit=True) as conn:
        conn.execute(sql.SQL("UPDATE {} SET sync_sequence=%s").format(g.metadata.table("_cammon_registry")),
                     (early["sync_sequence"],))
        conn.execute(sql.SQL("DELETE FROM {}").format(g.metadata.table("devices")))
    drain(g)
    assert remote_row(g, "devices", "id", device["id"])["name"] == "入口"


def test_small_batches_are_loadable_after_a_cold_boot(pg_gateway, tmp_path):
    g = pg_gateway
    device = g.create_device("入口")
    g.mkdir(device["id"], "day")
    token = create(g, device)
    g.close(token)
    # Coalescing moves the device change after child records in queue order.
    g.update_device(device["id"], {"retention_days": 7})
    g.settings.metadata_batch_size = 1
    for _ in range(5):
        if not g.db.metadata_state()["pending"]:
            break
        assert g.metadata.sync_once()
        g.metadata._disconnect()
        settings = g.settings.model_copy(update={"data_dir": tmp_path / ("boot_" + uuid.uuid4().hex)})
        rebooted = Gateway(settings)
        try:
            # SQLite foreign keys validate the committed PostgreSQL graph.
            assert not rebooted.db.all("PRAGMA foreign_key_check")
        finally:
            rebooted.shutdown()


def test_archive_preserves_old_sources_until_postgres_confirms_and_survives_memory_loss(pg_gateway, postgres, tmp_path):
    if not shutil.which("ffmpeg"):
        pytest.skip("Requires FFmpeg")
    from cammon.archive import Archiver
    from tests.test_archive import ingest, instant

    sample = tmp_path / "archive-source.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=blue:s=64x48:r=1",
                    "-t", "60", "-an", "-c:v", "libx264", "-threads", "1", str(sample)], check=True)
    g = pg_gateway
    device = g.create_device("归档摄像机", 1)
    g.mkdir(device["id"], "day")
    source_id = ingest(g, device, FIRST, sample.read_bytes())
    source_remote = g.recording(source_id)["remote_path"]
    archiver = Archiver(g)
    assert archiver.enqueue(instant("2025-10-09T02:00:00")) == 1
    job_id = g.db.one("SELECT id FROM archive_jobs")["id"]
    drain(g)
    postgres.stop()
    assert not g.metadata.sync_once()
    assert archiver.process(job_id)
    assert g.recording(source_id)["state"] == "stored"
    assert g.backend.exists(source_remote)
    settings = g.settings
    g.shutdown()  # Lose the successful but unsynchronized archive commit.
    postgres.start()
    rebooted = Gateway(settings)
    rebooted.backend = LocalBackend(tmp_path / "remote")
    try:
        archiver = Archiver(rebooted)
        assert rebooted.recording(source_id)["state"] == "stored"
        assert archiver.process(job_id), rebooted.db.one("SELECT error FROM archive_jobs WHERE id=?", (job_id,))
        assert rebooted.backend.exists(source_remote)
        drain(rebooted)
        archiver.collect_originals()
        drain(rebooted)
        assert not rebooted.backend.exists(source_remote)
        archived = rebooted.query_files({"kind": "archive"})["items"]
        assert len(archived) == 1 and rebooted.backend.exists(archived[0]["remote_path"])
        assert remote_row(rebooted, "recordings", "id", archived[0]["id"])["kind"] == "archive"
    finally:
        rebooted.shutdown()


def test_real_smb1_credentials_and_stored_reads_survive_empty_runtime(pg_gateway, tmp_path, monkeypatch):
    prefix = Path(os.environ.get("CAMMON_TEST_SAMBA_PREFIX", "/opt/samba"))
    if os.geteuid() != 0 or not (prefix / "sbin/smbd").exists():
        pytest.skip("Needs root and a compiled Samba/VFS build")
    import signal

    from impacket.smb import SMB_DIALECT
    from impacket.smb3structs import FILE_OPEN, GENERIC_READ
    from impacket.smbconnection import SMBConnection

    from cammon.rpc import Server
    from cammon.samba import SambaManager

    g = pg_gateway
    for path in (tmp_path, *tmp_path.parents):
        if path == Path("/tmp"):
            break
        path.chmod(0o755)
    g.settings.samba_prefix = prefix
    monkeypatch.setattr("cammon.core.make_backend", lambda *args: LocalBackend(tmp_path / "remote"))
    manager = SambaManager(g)
    g.samba = manager
    manager.prepare()
    device = g.create_device("原账号")
    g.mkdir(device["id"], "day")
    token = create(g, device)
    g.close(token)
    g.configure_storage({"protocol": "smb", "address": "smb://example/share", "username": "writer",
                         "password": "backend-secret"})
    g.seal_candidates(time.time() + 1900)
    assert g.flush_one(1)
    drain(g)
    settings = g.settings
    g.shutdown()
    shutil.rmtree(settings.data_dir)  # No Samba TDB, namespace, or metadata files remain.
    restored = Gateway(settings)
    rpc, process, client = None, None, None
    try:
        restored.samba = SambaManager(restored)
        restored.samba.prepare()
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        config = settings.samba_config.read_text().replace(
            "[global]", f"[global]\n    smb ports = {port}\n    interfaces = 127.0.0.1\n    bind interfaces only = yes")
        config = config.replace("log file = /dev/stdout", f"log file = {tmp_path / 'restored-samba.log'}")
        settings.samba_config.write_text(config)
        rpc = Server(restored)
        rpc.start()
        process = subprocess.Popen([str(prefix / "sbin/smbd"), "-F", "--no-process-group",
                                    "-s", str(settings.samba_config)], start_new_session=True,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                    break
            except OSError:
                time.sleep(0.05)
        client = SMBConnection("127.0.0.1", "127.0.0.1", sess_port=port, preferredDialect=SMB_DIALECT)
        client.login(device["username"], device["password"])
        tree = client.connectTree(device["share"])
        file_id = client.openFile(tree, FIRST.replace("/", "\\"), desiredAccess=GENERIC_READ,
                                  creationDisposition=FILE_OPEN)
        assert client.readFile(tree, file_id, 0, 10) == b"video-data"
        client.closeFile(tree, file_id)
    finally:
        if client:
            client.close()
        if process and process.poll() is None:
            os.killpg(process.pid, signal.SIGTERM)
            process.wait(timeout=10)
        if rpc:
            rpc.stop()
        restored.shutdown()
        subprocess.run(["userdel", device["username"]], capture_output=True)


def test_background_thread_connects_retries_and_stops(pg_gateway, postgres):
    g = pg_gateway
    g.metadata.start()
    deadline = time.monotonic() + 10
    while not g.metadata.connected and time.monotonic() < deadline:
        time.sleep(0.05)
    assert g.metadata.connected
    postgres.stop()
    deadline = time.monotonic() + 10
    while g.metadata.connected and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not g.metadata.connected
    g.set_setting("offline", {"value": True})
    postgres.start()
    deadline = time.monotonic() + 10
    while g.db.metadata_state()["pending"] and time.monotonic() < deadline:
        time.sleep(0.05)
    assert g.metadata.connected and g.db.metadata_state()["pending"] == 0
    g.metadata.stop()
    assert g.metadata.thread is None and g.metadata.connection is None
