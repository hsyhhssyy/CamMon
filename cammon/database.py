import json
import sqlite3
import threading
import time
import uuid

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS devices (
 id TEXT PRIMARY KEY, name TEXT NOT NULL, share TEXT NOT NULL UNIQUE, username TEXT NOT NULL UNIQUE,
 uid INTEGER NOT NULL, gid INTEGER NOT NULL, retention_days INTEGER NOT NULL DEFAULT 30,
 enabled INTEGER NOT NULL DEFAULT 1, created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS directories (
 id INTEGER PRIMARY KEY AUTOINCREMENT, device_id TEXT NOT NULL REFERENCES devices(id),
 path TEXT NOT NULL, modified_at REAL NOT NULL, UNIQUE(device_id,path)
);
CREATE TABLE IF NOT EXISTS recordings (
 id INTEGER PRIMARY KEY AUTOINCREMENT, device_id TEXT NOT NULL REFERENCES devices(id),
 path TEXT NOT NULL, channel TEXT NOT NULL, start REAL NOT NULL, end REAL NOT NULL, day TEXT NOT NULL,
 kind TEXT NOT NULL DEFAULT 'original', state TEXT NOT NULL DEFAULT 'cached', size INTEGER NOT NULL DEFAULT 0,
 created_at REAL NOT NULL, last_write REAL NOT NULL, successor INTEGER NOT NULL DEFAULT 0,
 remote_path TEXT, sha256 TEXT, error TEXT, retry_at REAL NOT NULL DEFAULT 0,
 duration REAL, job_id TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS live_path ON recordings(device_id,path,kind) WHERE state!='deleted';
CREATE INDEX IF NOT EXISTS recording_filters ON recordings(state,kind,day,device_id,channel);
CREATE INDEX IF NOT EXISTS channel_timeline ON recordings(device_id,channel,start);
CREATE TABLE IF NOT EXISTS archive_jobs (
 id TEXT PRIMARY KEY, device_id TEXT NOT NULL, channel TEXT NOT NULL, day TEXT NOT NULL,
 sources TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'queued', error TEXT,
 created_at REAL NOT NULL, updated_at REAL NOT NULL, retry_at REAL NOT NULL DEFAULT 0,
 archive_id INTEGER, attempts INTEGER NOT NULL DEFAULT 0, output_size INTEGER, output_sha TEXT
);
CREATE TABLE IF NOT EXISTS coverage (
 recording_id INTEGER NOT NULL REFERENCES recordings(id), day TEXT NOT NULL,
 job_id TEXT NOT NULL REFERENCES archive_jobs(id), PRIMARY KEY(recording_id,day)
);
CREATE TABLE IF NOT EXISTS sessions (token_hash TEXT PRIMARY KEY, expires REAL NOT NULL);
PRAGMA user_version=1;
"""

# PostgreSQL is the persistent store. These tables and the coalescing change
# queue exist only in RAM and can be discarded with the process.
METADATA_TABLES = {
    "settings": ("key",), "devices": ("id",), "directories": ("id",),
    "recordings": ("id",), "archive_jobs": ("id",),
    "coverage": ("recording_id", "day"), "sessions": ("token_hash",),
}
TRACKING_SCHEMA = """
CREATE TABLE IF NOT EXISTS _meta_state (
 id INTEGER PRIMARY KEY CHECK(id=1), gateway_id TEXT NOT NULL,
 revision INTEGER NOT NULL DEFAULT 0, last_ack INTEGER NOT NULL DEFAULT 0, last_sync REAL,
 last_commit INTEGER NOT NULL DEFAULT 0, tracking INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS _meta_outbox (
 table_name TEXT NOT NULL, row_key TEXT NOT NULL, revision INTEGER NOT NULL,
 PRIMARY KEY(table_name,row_key)
);
CREATE INDEX IF NOT EXISTS _meta_pending ON _meta_outbox(revision);
"""


class Database:
    def __init__(self):
        self.lock = threading.RLock()
        self.connection = sqlite3.connect(":memory:", check_same_thread=False, isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA temp_store=MEMORY")
        self.connection.execute("PRAGMA journal_mode=MEMORY")
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.connection.executescript(SCHEMA)
        self._install_tracking()

    def execute(self, sql: str, args=()):
        with self.lock:
            return self.connection.execute(sql, args)

    def one(self, sql: str, args=()) -> dict | None:
        with self.lock:
            row = self.connection.execute(sql, args).fetchone()
            return dict(row) if row else None

    def all(self, sql: str, args=()) -> list[dict]:
        with self.lock:
            return [dict(row) for row in self.connection.execute(sql, args).fetchall()]

    def close(self):
        with self.lock:
            self.connection.close()

    def _install_tracking(self):
        self.connection.executescript(TRACKING_SCHEMA)
        first = not self.one("SELECT id FROM _meta_state")
        if first:
            self.execute("INSERT INTO _meta_state(id,gateway_id) VALUES (1,?)", (uuid.uuid4().hex,))
        for table, keys in METADATA_TABLES.items():
            def dirty_row(prefix):
                key = "json_array(" + ",".join(f'{prefix}."{k}"' for k in keys) + ")"
                return (
                    "INSERT INTO _meta_outbox(table_name,row_key,revision) "
                    f"VALUES ('{table}',{key},(SELECT revision FROM _meta_state WHERE id=1)) "
                    "ON CONFLICT(table_name,row_key) DO UPDATE SET revision=excluded.revision;"
                )
            for operation, prefix in (("INSERT", "NEW"), ("UPDATE", "NEW"), ("DELETE", "OLD")):
                # Primary-key changes also queue deletion of the previous key.
                previous = dirty_row("OLD") if operation == "UPDATE" else ""
                self.connection.executescript(
                    f'CREATE TRIGGER IF NOT EXISTS "_meta_{table}_{operation}" AFTER {operation} ON "{table}" '
                    "WHEN (SELECT tracking FROM _meta_state WHERE id=1)=1 "
                    "BEGIN UPDATE _meta_state SET revision=revision+1 WHERE id=1;"
                    + previous + dirty_row(prefix) + " END;"
                )
        if first:
            self.seed_metadata()

    def metadata_state(self) -> dict:
        return self.one("SELECT *, (SELECT count(*) FROM _meta_outbox) AS pending FROM _meta_state WHERE id=1")

    def metadata_pending(self, table: str, key: list) -> bool:
        placeholders = ",".join("?" for _ in key)
        row_key = self.one(f"SELECT json_array({placeholders}) AS k", key)["k"]
        return self.one("SELECT 1 FROM _meta_outbox WHERE table_name=? AND row_key=?", (table, row_key)) is not None

    def seed_metadata(self):
        """Queue a full, idempotent snapshot for initial attach / remote restore."""
        with self.lock:
            self.execute("BEGIN IMMEDIATE")
            try:
                self.execute("UPDATE _meta_state SET revision=revision+1 WHERE id=1")
                for table, keys in METADATA_TABLES.items():
                    key = "json_array(" + ",".join(f'"{k}"' for k in keys) + ")"
                    self.execute(
                        "INSERT INTO _meta_outbox(table_name,row_key,revision) "
                        f"SELECT '{table}',{key},(SELECT revision FROM _meta_state WHERE id=1) "
                        f'FROM "{table}" WHERE 1=1 ON CONFLICT(table_name,row_key) '
                        "DO UPDATE SET revision=excluded.revision"
                    )
                self.execute("COMMIT")
            except BaseException:
                self.execute("ROLLBACK")
                raise

    def metadata_columns(self) -> dict[str, list[dict]]:
        return {table: self.all(f'PRAGMA table_info("{table}")') for table in METADATA_TABLES}

    def load_metadata(self, snapshot: dict[str, list[dict]], registry: dict):
        """Hydrate a fresh RAM index from one consistent PostgreSQL snapshot."""
        with self.lock:
            self.execute("BEGIN IMMEDIATE")
            try:
                self.execute("UPDATE _meta_state SET tracking=0 WHERE id=1")
                for table in reversed(METADATA_TABLES):
                    self.execute(f'DELETE FROM "{table}"')
                self.execute("DELETE FROM sqlite_sequence")
                for table, rows in snapshot.items():
                    for row in rows:
                        names = ",".join(f'"{name}"' for name in row)
                        placeholders = ",".join("?" for _ in row)
                        self.execute(f'INSERT INTO "{table}" ({names}) VALUES ({placeholders})', tuple(row.values()))
                self.execute("DELETE FROM _meta_outbox")
                self.execute("UPDATE _meta_state SET gateway_id=?,revision=?,last_ack=?,last_commit=?,"
                             "last_sync=?,tracking=1 WHERE id=1",
                             (registry["gateway_id"], registry["local_revision"], registry["local_revision"],
                              registry["sync_sequence"], registry["last_sync"]))
                self.execute("COMMIT")
            except BaseException:
                self.execute("ROLLBACK")
                raise

    def metadata_batch(self, limit: int) -> list[dict]:
        # Copy rows and revision together. Never hold this lock during network I/O.
        with self.lock:
            pending = self.all("SELECT * FROM _meta_outbox ORDER BY revision,table_name,row_key LIMIT ?", (limit,))
            changes = {}
            revision = self.metadata_state()["revision"]

            def include(table, key):
                placeholders = ",".join("?" for _ in key)
                row_key = self.one(f"SELECT json_array({placeholders}) AS k", key)["k"]
                identity = (table, row_key)
                if identity in changes:
                    return
                change = self.one("SELECT * FROM _meta_outbox WHERE table_name=? AND row_key=?", identity) or {
                    "table_name": table, "row_key": row_key, "revision": revision,
                }
                keys = METADATA_TABLES[table]
                where = " AND ".join(f'"{k}"=?' for k in keys)
                row = self.one(f'SELECT * FROM "{table}" WHERE {where}', key)
                change["row"] = row
                change["key"] = key
                changes[identity] = change
                # Persist related parents and credentials in the same remote
                # transaction so a cold boot can load any committed batch.
                if row:
                    if table == "devices":
                        credential = "credential:" + row["id"]
                        if self.one("SELECT key FROM settings WHERE key=?", (credential,)):
                            include("settings", [credential])
                    if table in ("directories", "recordings", "archive_jobs"):
                        include("devices", [row["device_id"]])
                    if table == "coverage":
                        include("recordings", [row["recording_id"]])
                        include("archive_jobs", [row["job_id"]])
                    if table == "archive_jobs":
                        for source in json.loads(row["sources"]):
                            include("recordings", [source["id"] if isinstance(source, dict) else source])
                        if row["archive_id"] is not None:
                            include("recordings", [row["archive_id"]])

            for change in pending:
                include(change["table_name"], json.loads(change["row_key"]))
            order = {table: index for index, table in enumerate(METADATA_TABLES)}
            return sorted(changes.values(), key=lambda c: order[c["table_name"]])

    def acknowledge_metadata(self, changes: list[dict], timestamp: float | None = None,
                             commit_sequence: int | None = None):
        # A row updated during upload has a newer revision and remains queued.
        with self.lock:
            self.execute("BEGIN IMMEDIATE")
            try:
                for change in changes:
                    self.execute("DELETE FROM _meta_outbox WHERE table_name=? AND row_key=? AND revision=?",
                                 (change["table_name"], change["row_key"], change["revision"]))
                revision = max((c["revision"] for c in changes), default=0)
                self.execute("UPDATE _meta_state SET last_ack=max(last_ack,?),last_sync=?,"
                             "last_commit=coalesce(?,last_commit) WHERE id=1",
                             (revision, time.time() if timestamp is None else timestamp, commit_sequence))
                self.execute("COMMIT")
            except BaseException:
                self.execute("ROLLBACK")
                raise
