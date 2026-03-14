"""
Backup engine: streams mysqldump / pg_dump output through gzip compression
into a file-like object that can be piped directly to S3 without touching disk.

Architecture
------------
  Subprocess (dump) --> OS pipe --> GzipFile (compress) --> readable end
  ↑ producer thread                                         ↑ S3 uploader reads here

Using an OS pipe keeps memory usage proportional to the pipe buffer (~64 KB),
not the full database size.
"""
import gzip
import io
import os
import subprocess
import threading
from urllib.parse import urlparse


def _parse_url(db_url: str) -> dict:
    p = urlparse(db_url)
    return {
        "scheme": p.scheme.lower().split("+")[0],  # mysql / postgresql
        "host": p.hostname or "127.0.0.1",
        "port": p.port,
        "user": p.username or "",
        "password": p.password or "",
        "database": (p.path or "").lstrip("/"),
    }


class _StreamingGzip(io.RawIOBase):
    """
    Readable binary stream backed by a subprocess piped through gzip.

    The producer thread reads raw SQL from the dump process, compresses it
    with gzip, and writes into the write end of an OS pipe.
    The consumer (S3 uploader) reads from the read end of that pipe.
    """

    CHUNK = 64 * 1024  # 64 KB read chunks

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

    # ---- io.RawIOBase interface ----

    def readinto(self, b):
        data = self._reader.read(len(b))
        if not data:
            # EOF on the pipe — check whether the producer failed
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
# Public API
# ---------------------------------------------------------------------------

def get_dump_stream(db_url: str) -> tuple[io.RawIOBase, str]:
    """
    Returns ``(stream, db_type)`` where *stream* is a readable binary stream
    of gzip-compressed SQL and *db_type* is ``"mysql"`` or ``"postgresql"``.

    Raises ``ValueError`` for unsupported DB schemes.
    Raises ``RuntimeError`` if the dump process fails.
    """
    db = _parse_url(db_url)

    if db["scheme"] == "mysql":
        process = _start_mysqldump(db)
        return _StreamingGzip(process), "mysql"

    if db["scheme"] in ("postgresql", "postgres"):
        process = _start_pgdump(db)
        return _StreamingGzip(process), "postgresql"

    raise ValueError(f"Unsupported database scheme: {db['scheme']!r}")


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _start_mysqldump(db: dict) -> subprocess.Popen:
    port = db["port"] or 3306

    # SECURITY: pass password via MYSQL_PWD — never via -p<pwd> on the
    # command line, which would expose it in `ps aux` / /proc/<pid>/cmdline.
    env = os.environ.copy()
    env["MYSQL_PWD"] = db["password"]

    cmd = [
        "mysqldump",
        f"-h{db['host']}",
        f"-P{port}",
        f"-u{db['user']}",
        # No -p flag: password supplied through env var above
        "--single-transaction",
        "--quick",
        "--lock-tables=false",
        "--set-gtid-purged=OFF",
        db["database"],
    ]
    return subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )


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
