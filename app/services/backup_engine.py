"""
Backup engine — streams a gzip-compressed SQL dump to a readable binary
stream that can be piped directly to S3 without touching disk.

MySQL strategy (no mysqldump binary required)
─────────────────────────────────────────────
Uses PyMySQL (pure Python) to connect, read schema + data, and emit
standard SQL statements in a background thread compressed on-the-fly.

PostgreSQL strategy
───────────────────
Uses pg_dump subprocess (still required for Postgres).
"""
import gzip
import io
import logging
import os
import subprocess
import threading
from urllib.parse import urlparse

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# URL parser
# ---------------------------------------------------------------------------

def _parse_url(db_url: str) -> dict:
    p = urlparse(db_url)
    return {
        "scheme":   p.scheme.lower().split("+")[0],  # mysql / postgresql
        "host":     p.hostname or "127.0.0.1",
        "port":     p.port,
        "user":     p.username or "",
        "password": p.password or "",
        "database": (p.path or "").lstrip("/"),
    }


# ---------------------------------------------------------------------------
# Generic streaming gzip wrapper (used by the pg_dump subprocess path)
# ---------------------------------------------------------------------------

class _StreamingGzip(io.RawIOBase):
    """
    Readable binary stream backed by a subprocess piped through gzip.

    The producer thread reads raw SQL from the dump process, compresses it
    with gzip, and writes into the write end of an OS pipe.
    The consumer (S3 uploader) reads from the read end of that pipe.
    """

    CHUNK = 64 * 1024

    def __init__(self, process: subprocess.Popen):
        super().__init__()
        self._process = process
        self._error: Exception | None = None

        r_fd, w_fd = os.pipe()
        self._reader = io.open(r_fd, "rb", buffering=0)
        self._writer = io.open(w_fd, "wb", buffering=0)

        self._thread = threading.Thread(target=self._produce, daemon=True)
        self._thread.start()

    def _produce(self):
        try:
            with gzip.open(self._writer, "wb", compresslevel=6) as gz:
                while True:
                    chunk = self._process.stdout.read(self.CHUNK)
                    if not chunk:
                        break
                    gz.write(chunk)
        except Exception as exc:
            self._error = exc
        finally:
            try:
                self._writer.close()
            except Exception:
                pass

        self._process.wait()
        if self._process.returncode != 0:
            stderr = b""
            try:
                stderr = self._process.stderr.read()
            except Exception:
                pass
            if not self._error:
                self._error = RuntimeError(
                    f"Dump process exited with code {self._process.returncode}: "
                    f"{stderr.decode('utf-8', errors='replace')[:500]}"
                )

    def readinto(self, b):
        data = self._reader.read(len(b))
        if not data:
            self._thread.join(timeout=5)
            if self._error:
                raise self._error
            return 0
        n = len(data)
        b[:n] = data
        return n

    def readable(self):
        return True

    def close(self):
        try:
            self._reader.close()
        except Exception:
            pass
        super().close()


# ---------------------------------------------------------------------------
# Pure-Python MySQL dump stream (no mysqldump binary needed)
# ---------------------------------------------------------------------------

class _PyMySQLDumpStream(io.RawIOBase):
    """
    Readable binary stream that emits a gzip-compressed SQL dump of a MySQL
    database using PyMySQL. Runs entirely in Python — no external binary.

    A background thread iterates over every table, emits:
      - SET statements (character set, foreign keys off)
      - DROP TABLE IF EXISTS / CREATE TABLE
      - INSERT INTO ... VALUES (...) in batches of 500 rows
      - COMMIT
    and writes compressed bytes into an OS pipe that the caller reads.
    """

    BATCH = 500  # rows per INSERT statement

    def __init__(self, db: dict):
        super().__init__()
        self._db = db
        self._error: Exception | None = None

        r_fd, w_fd = os.pipe()
        self._reader = io.open(r_fd, "rb", buffering=0)
        self._writer = io.open(w_fd, "wb", buffering=0)

        self._thread = threading.Thread(target=self._produce, daemon=True)
        self._thread.start()

    # ── producer runs in background thread ───────────────────────────────────

    def _produce(self):
        import pymysql
        import pymysql.cursors

        conn = None
        try:
            conn = pymysql.connect(
                host=self._db["host"],
                port=self._db["port"] or 3306,
                user=self._db["user"],
                password=self._db["password"],
                database=self._db["database"],
                charset="utf8mb4",
                cursorclass=pymysql.cursors.SSCursor,  # server-side cursor = low RAM
                connect_timeout=30,
            )

            with gzip.open(self._writer, "wb", compresslevel=6) as gz:
                def w(line: str):
                    gz.write((line + "\n").encode("utf-8"))

                # ── header ────────────────────────────────────────────────
                w("-- JHBridge MySQL Backup")
                w(f"-- Database: {self._db['database']}")
                w("-- Generated by backup_engine (PyMySQL)")
                w("")
                w("SET NAMES utf8mb4;")
                w("SET FOREIGN_KEY_CHECKS=0;")
                w("SET SQL_MODE='NO_AUTO_VALUE_ON_ZERO';")
                w("SET AUTOCOMMIT=0;")
                w("START TRANSACTION;")
                w("")

                # ── list tables ───────────────────────────────────────────
                with conn.cursor() as cur:
                    cur.execute("SHOW FULL TABLES WHERE Table_type = 'BASE TABLE'")
                    tables = [row[0] for row in cur.fetchall()]

                log.info("[pymysql_dump] Found %d tables", len(tables))

                for table in tables:
                    self._dump_table(conn, gz, w, table)

                w("")
                w("COMMIT;")
                w("SET FOREIGN_KEY_CHECKS=1;")

        except Exception as exc:
            log.error("[pymysql_dump] Producer error: %s", exc)
            self._error = exc
        finally:
            if conn:
                try:
                    conn.close()
                except Exception:
                    pass
            try:
                self._writer.close()
            except Exception:
                pass

    def _dump_table(self, conn, gz, w, table: str):
        import pymysql
        import pymysql.cursors

        log.info("[pymysql_dump] Dumping table: %s", table)

        # ── CREATE TABLE ──────────────────────────────────────────────────
        with conn.cursor() as cur:
            cur.execute(f"SHOW CREATE TABLE `{table}`")
            row = cur.fetchone()
            create_sql = row[1] if row else f"-- CREATE TABLE {table} not available"

        w(f"-- Table: {table}")
        w(f"DROP TABLE IF EXISTS `{table}`;")
        w(create_sql + ";")
        w("")

        # ── Data ──────────────────────────────────────────────────────────
        # Use a fresh SSCursor per table to avoid mixing result sets.
        with conn.cursor(pymysql.cursors.SSCursor) as cur:
            cur.execute(f"SELECT * FROM `{table}`")

            columns = [desc[0] for desc in cur.description]
            col_list = ", ".join(f"`{c}`" for c in columns)

            batch = []
            row_count = 0

            for row in cur:
                batch.append(row)
                row_count += 1
                if len(batch) >= self.BATCH:
                    self._write_insert(w, table, col_list, batch)
                    batch = []

            if batch:
                self._write_insert(w, table, col_list, batch)

        log.info("[pymysql_dump]   → %d rows", row_count)
        w("")

    @staticmethod
    def _write_insert(w, table: str, col_list: str, rows: list):
        values_parts = []
        for row in rows:
            parts = []
            for val in row:
                if val is None:
                    parts.append("NULL")
                elif isinstance(val, (int, float)):
                    parts.append(str(val))
                elif isinstance(val, bytes):
                    hex_str = val.hex()
                    parts.append(f"0x{hex_str}")
                else:
                    # Escape single quotes and backslashes
                    escaped = str(val).replace("\\", "\\\\").replace("'", "\\'")
                    parts.append(f"'{escaped}'")
            values_parts.append(f"({', '.join(parts)})")

        w(f"INSERT INTO `{table}` ({col_list}) VALUES")
        w(",\n".join(values_parts) + ";")

    # ── io.RawIOBase interface ────────────────────────────────────────────────

    def readinto(self, b):
        data = self._reader.read(len(b))
        if not data:
            self._thread.join(timeout=10)
            if self._error:
                raise self._error
            return 0
        n = len(data)
        b[:n] = data
        return n

    def readable(self):
        return True

    def close(self):
        try:
            self._reader.close()
        except Exception:
            pass
        super().close()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_dump_stream(db_url: str) -> tuple[io.RawIOBase, str]:
    """
    Returns ``(stream, db_type)`` where *stream* is a readable binary stream
    of gzip-compressed SQL and *db_type* is ``"mysql"`` or ``"postgresql"``.

    MySQL  → pure Python via PyMySQL (no mysqldump binary needed)
    Postgres → pg_dump subprocess
    """
    db = _parse_url(db_url)

    if db["scheme"] == "mysql":
        log.info("[backup_engine] Using PyMySQL pure-Python dump for MySQL")
        return _PyMySQLDumpStream(db), "mysql"

    if db["scheme"] in ("postgresql", "postgres"):
        log.info("[backup_engine] Using pg_dump subprocess for PostgreSQL")
        process = _start_pgdump(db)
        return _StreamingGzip(process), "postgresql"

    raise ValueError(f"Unsupported database scheme: {db['scheme']!r}")


# ---------------------------------------------------------------------------
# PostgreSQL helper (pg_dump subprocess)
# ---------------------------------------------------------------------------

def _start_pgdump(db: dict) -> subprocess.Popen:
    port = db["port"] or 5432
    env = os.environ.copy()
    env["PGPASSWORD"] = db["password"]

    cmd = [
        "pg_dump",
        "-h", db["host"],
        "-p", str(port),
        "-U", db["user"],
        "--no-password",
        db["database"],
    ]
    return subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )
