"""PostgreSQL persistence and a process-local memory cache.

In-memory SQLite triggers enqueue row keys in the same transaction as the change.
Repeated camera writes coalesce; PostgreSQL commits before local acknowledgement.
Startup loads PostgreSQL once. Background synchronization handles later changes;
camera / API / archive operations use the memory index throughout an outage.
"""

import hashlib
import threading
import time

import psycopg
from psycopg import rows, sql

from cammon.config import Settings
from cammon.database import METADATA_TABLES, Database

SCHEMA_VERSION = 1


class MetadataConflictError(Exception):
    pass


class MetadataBootstrapError(Exception):
    pass


class MetadataSynchronizer:
    def __init__(self, db: Database, settings: Settings):
        self.db = db
        self.settings = settings
        self.configured = bool(settings.postgres_dsn and settings.postgres_dsn.get_secret_value())
        self.connected = False
        self.error: str | None = None
        self.connection: psycopg.Connection | None = None
        self.stopping = threading.Event()
        self.thread: threading.Thread | None = None
        self.sync_lock = threading.Lock()
        self.last_batch_full = False
        self.schema = settings.postgres_schema
        self.columns = db.metadata_columns()
        # Only one live PostgreSQL session may write a gateway's schema.
        digest = hashlib.sha256(("cammon-metadata:" + self.schema).encode()).digest()[:8]
        self.advisory_key = int.from_bytes(digest, "big", signed=True)

    def table(self, name: str):
        return sql.Identifier(self.schema, name)

    def _connect(self, bootstrap: bool = False):
        timeout_ms = self.settings.metadata_statement_timeout_seconds * 1000
        conn = psycopg.connect(
            self.settings.postgres_dsn.get_secret_value(), autocommit=True,
            connect_timeout=self.settings.metadata_connect_timeout_seconds,
            application_name="cammon-metadata", tcp_user_timeout=timeout_ms,
            keepalives=1, keepalives_idle=5, keepalives_interval=2, keepalives_count=2,
            options=f"-c statement_timeout={timeout_ms} -c lock_timeout={timeout_ms}",
        )
        self.connection = conn
        registry = self.table("_cammon_registry")
        local = self.db.metadata_state()
        reset = False
        with conn.transaction():
            exists = conn.execute("SELECT 1 FROM pg_namespace WHERE nspname=%s", (self.schema,)).fetchone()
            if not exists:
                conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(self.schema)))
            if not conn.execute("SELECT pg_try_advisory_lock(%s)", (self.advisory_key,)).fetchone()[0]:
                raise MetadataConflictError("此 PostgreSQL schema 已被另一个 CamMon 实例使用")
            conn.execute(sql.SQL(
                "CREATE TABLE IF NOT EXISTS {} (id SMALLINT PRIMARY KEY CHECK(id=1), "
                "gateway_id TEXT NOT NULL, schema_version INTEGER NOT NULL, "
                "local_revision BIGINT NOT NULL DEFAULT 0, last_sync DOUBLE PRECISION, "
                "sync_sequence BIGINT NOT NULL DEFAULT 0)"
            ).format(registry))
            row = conn.execute(sql.SQL(
                "SELECT gateway_id,schema_version,local_revision,sync_sequence FROM {} WHERE id=1 FOR UPDATE"
            ).format(registry)).fetchone()
            if row:
                if not bootstrap and row[0] != local["gateway_id"]:
                    raise MetadataConflictError("此 PostgreSQL schema 属于另一份本地数据，已停止同步以避免覆盖")
                if row[1] != SCHEMA_VERSION:
                    raise MetadataConflictError("PostgreSQL 元数据版本不兼容")
                if not bootstrap and row[2] > local["revision"]:
                    raise MetadataConflictError("PostgreSQL 比运行中的内存副本更新，已停止同步以避免覆盖")
                # Several snapshot batches may share one local row revision.
                # The independent commit sequence detects restores between them.
                reset = not bootstrap and (row[2] < local["last_ack"] or row[3] < local["last_commit"])
            else:
                for table in METADATA_TABLES:
                    if conn.execute("SELECT to_regclass(%s)", (f"{self.schema}.{table}",)).fetchone()[0]:
                        raise MetadataConflictError("目标 schema 已有未归属 CamMon 的同名表，请配置专用空 schema")
                conn.execute(sql.SQL("INSERT INTO {}(id,gateway_id,schema_version) VALUES(1,%s,%s)")
                             .format(registry), (local["gateway_id"], SCHEMA_VERSION))
                reset = True
            types = {"INTEGER": "BIGINT", "REAL": "DOUBLE PRECISION", "TEXT": "TEXT"}
            for table, columns in self.columns.items():
                exists = conn.execute("SELECT to_regclass(%s)", (f"{self.schema}.{table}",)).fetchone()[0]
                if bootstrap and row and exists is None:
                    raise MetadataConflictError("PostgreSQL 元数据表不完整，无法加载已有配置")
                reset |= exists is None
                definitions = [sql.SQL("{} {}{}").format(
                    sql.Identifier(c["name"]), sql.SQL(types[c["type"]]),
                    sql.SQL(" NOT NULL" if c["notnull"] or c["pk"] else ""),
                ) for c in columns]
                definitions.extend([
                    sql.SQL("_cammon_revision BIGINT NOT NULL"),
                    sql.SQL("PRIMARY KEY ({})").format(
                        sql.SQL(",").join(map(sql.Identifier, METADATA_TABLES[table]))),
                ])
                conn.execute(sql.SQL("CREATE TABLE IF NOT EXISTS {} ({})").format(
                    self.table(table), sql.SQL(",").join(definitions)))
            conn.execute(sql.SQL("CREATE INDEX IF NOT EXISTS recording_filters ON {} "
                                 "(state,kind,day,device_id,channel)").format(self.table("recordings")))
        if bootstrap and row:
            # All related tables come from one MVCC snapshot. No old memory
            # index is available after a process restart, and none is persisted.
            with conn.transaction():
                conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
                with conn.cursor(row_factory=rows.dict_row) as cursor:
                    registry_row = cursor.execute(sql.SQL("SELECT * FROM {} WHERE id=1").format(registry)).fetchone()
                    snapshot = {}
                    for table, columns in self.columns.items():
                        names = sql.SQL(",").join(sql.Identifier(c["name"]) for c in columns)
                        snapshot[table] = cursor.execute(sql.SQL("SELECT {} FROM {}").format(
                            names, self.table(table))).fetchall()
            self.db.load_metadata(snapshot, registry_row)
        if reset:
            # During the same process an older remote restore can be rebuilt
            # from memory. Startup always treats PostgreSQL as its data source.
            self.db.seed_metadata()
            with conn.transaction():
                conn.execute(sql.SQL("TRUNCATE {}").format(
                    sql.SQL(",").join(self.table(t) for t in METADATA_TABLES)))
                conn.execute(sql.SQL("UPDATE {} SET local_revision=0,sync_sequence=0 WHERE id=1").format(registry))

    @staticmethod
    def error_message(exc: Exception, fallback: bool = True) -> str:
        if isinstance(exc, MetadataConflictError):
            return str(exc)
        code = getattr(exc, "sqlstate", None)
        detail = type(exc).__name__ + (f", SQLSTATE {code}" if code else "")
        context = "，正在使用内存副本" if fallback else ""
        return f"PostgreSQL 同步失败{context}（{detail}）"

    def bootstrap(self):
        if not self.configured:
            return
        try:
            self._connect(bootstrap=True)
            self.connected = True
        except Exception as exc:
            self._disconnect()
            raise MetadataBootstrapError("启动时需要可用的 PostgreSQL；" + self.error_message(exc, fallback=False)) from None

    def _apply(self, conn, changes: list[dict]):
        for change in changes:
            table = change["table_name"]
            keys = METADATA_TABLES[table]
            if change["row"] is None:
                where = sql.SQL(" AND ").join(sql.SQL("{}=%s").format(sql.Identifier(k)) for k in keys)
                conn.execute(sql.SQL("DELETE FROM {} WHERE {} AND _cammon_revision<=%s").format(
                    self.table(table), where), (*change["key"], change["revision"]))
                continue
            row = change["row"]
            names = list(row) + ["_cammon_revision"]
            assignments = sql.SQL(",").join(sql.SQL("{}=EXCLUDED.{}").format(
                sql.Identifier(name), sql.Identifier(name)) for name in names if name not in keys)
            conn.execute(sql.SQL(
                "INSERT INTO {} AS target ({}) VALUES ({}) ON CONFLICT ({}) DO UPDATE SET {} "
                "WHERE target._cammon_revision<=EXCLUDED._cammon_revision"
            ).format(self.table(table), sql.SQL(",").join(map(sql.Identifier, names)),
                     sql.SQL(",").join(sql.Placeholder() for _ in names),
                     sql.SQL(",").join(map(sql.Identifier, keys)), assignments),
                         (*row.values(), change["revision"]))

    def sync_once(self) -> bool:
        if not self.configured or self.stopping.is_set():
            return False
        with self.sync_lock:
            try:
                if self.connection is None:
                    self._connect()
                conn = self.connection
                changes = self.db.metadata_batch(self.settings.metadata_batch_size)
                self.last_batch_full = len(changes) >= self.settings.metadata_batch_size
                now = time.time()
                with conn.transaction():
                    self._apply(conn, changes)
                    revision = max((c["revision"] for c in changes), default=0)
                    sequence = conn.execute(sql.SQL(
                        "UPDATE {} SET local_revision=GREATEST(local_revision,%s),last_sync=%s,"
                        "sync_sequence=sync_sequence+%s WHERE id=1 RETURNING sync_sequence"
                    ).format(self.table("_cammon_registry")), (revision, now, int(bool(changes)))).fetchone()[0]
                # Lost acknowledgement replays the same primary keys while this
                # process lives. A process restart discards unsynchronized changes.
                self.db.acknowledge_metadata(changes, now, sequence)
                self.connected = True
                self.error = None
                return True
            except Exception as exc:
                self.connected = False
                # Expose no raw driver message containing credentials / a DSN.
                self.error = self.error_message(exc)
                self._disconnect()
                return False

    def _disconnect(self):
        if self.connection:
            self.connection.close()
            self.connection = None

    def status(self) -> dict:
        state = self.db.metadata_state()
        return {"configured": self.configured, "connected": self.connected,
                "schema": self.schema, "pending_rows": state["pending"] if self.configured else 0,
                "last_sync": state["last_sync"], "error": self.error}

    def start(self):
        if self.configured and self.thread is None:
            self.stopping.clear()
            self.thread = threading.Thread(target=self._run, name="cammon-postgres", daemon=True)
            self.thread.start()

    def _run(self):
        while not self.stopping.is_set():
            success = self.sync_once()
            # Drain a backlog without sleeping between batches. Camera writes
            # remain free to acquire the local lock throughout PostgreSQL I/O.
            if success and self.last_batch_full and self.db.metadata_state()["pending"]:
                continue
            self.stopping.wait(self.settings.metadata_sync_seconds)
        self._disconnect()

    def stop(self):
        self.stopping.set()
        conn = self.connection
        if conn:
            try:
                conn.cancel()
            except psycopg.Error:
                pass
        if self.thread:
            self.thread.join()
            self.thread = None
        else:
            with self.sync_lock:
                self._disconnect()
        self.connected = False
