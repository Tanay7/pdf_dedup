#!/usr/bin/env python3
"""DupeFinder: find duplicate and near-duplicate PDFs and photos by content.

Scans folders or whole drives (for example C:\\ and D:\\), scores every match,
groups burst shots and suggests the sharpest one, flags blurry or badly
exposed photos, and opens a browser report served only on this computer.
Files you choose go to the Recycle Bin (or to a _duplicates_review folder on
USB and network drives). Nothing is ever deleted permanently, and the tool
never connects to the internet.

Typical runs:

    python dupefinder.py C:\\ D:\\
    python dupefinder.py D:\\Photos --types image
    python dupefinder.py D:\\Docs --types pdf --min-score 90 --report-only
    python dupefinder.py --undo-review

Run ``python dupefinder.py --help`` for every option.
"""

from __future__ import annotations

import argparse
import collections
import concurrent.futures
import csv
import datetime as dt
import hashlib
import http.server
import io
import ipaddress
import json
import logging
import math
import multiprocessing
import multiprocessing.connection
import os
import re
import secrets
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata
import urllib.parse
import warnings
import webbrowser
from dataclasses import dataclass, field
from pathlib import Path

try:
    import numpy as np
except ImportError:  # reported by check_dependencies()
    np = None
try:
    from PIL import Image, ImageOps
except ImportError:
    Image = ImageOps = None

__version__ = "1.0.0"

APP_NAME = "DupeFinder"
IS_WINDOWS = os.name == "nt"
IS_MAC = sys.platform == "darwin"

# Raising this forces every cached fingerprint to be recomputed.
ALGO_VERSION = 2

PDF_EXTS = {".pdf"}
RAW_EXTS = {".dng", ".cr2", ".cr3", ".crw", ".nef", ".nrw", ".arw", ".srf", ".sr2", ".orf",
            ".rw2", ".raf", ".pef", ".srw", ".rwl", ".3fr", ".iiq", ".erf", ".kdc", ".mos"}
HEIF_EXTS = {".heic", ".heif", ".hif"}
IMAGE_EXTS = ({".jpg", ".jpeg", ".jpe", ".jfif", ".png", ".webp", ".avif", ".gif", ".bmp",
               ".tif", ".tiff"} | HEIF_EXTS | RAW_EXTS)
BROWSER_IMAGE_TYPES = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".jpe": "image/jpeg",
                       ".jfif": "image/jpeg", ".png": "image/png", ".webp": "image/webp",
                       ".avif": "image/avif", ".gif": "image/gif", ".bmp": "image/bmp"}

REDUCED_SIZE = 1024          # photos are analysed at about this long side
HASH_BITS = 64
BURST_BITS = 16              # most differing bits between neighbouring burst shots
PIXEL_CANDIDATE_BITS = 4     # pixel fingerprints for pairs this close (see notes)
FLAT_STD = 2.0               # grey std below this on the 32x32 copy = flat image
SCANNED_CHARS_PER_PAGE = 20
SHINGLE_WORDS = 5
MIN_SHINGLES = 5
MINHASH_PERM = 128
LSH_RECALL = 0.98
REVIEW_DIR = "_duplicates_review"
DEFAULT_BLUR_CUTOFF = 45.0   # on the 0-100 sharpness score; tune with --blur-cutoff
MAX_POOR_LISTED = 2000       # possibly poor shots listed in the report (worst first)
COLOUR_DIFF_CUT = 28.0       # chroma difference (0-255 scale) above which photos differ in colour
WORKER_CRASH = "The worker process"   # errors starting with this are retried next scan
IDLE_TIMEOUT = 30 * 60
READ_CHUNK = 1 << 20

# Windows file attributes of OneDrive (and other cloud) online-only files.
FILE_ATTRIBUTE_OFFLINE = 0x1000
FILE_ATTRIBUTE_RECALL_ON_OPEN = 0x40000
FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS = 0x400000
ONLINE_ONLY_MASK = (FILE_ATTRIBUTE_OFFLINE | FILE_ATTRIBUTE_RECALL_ON_OPEN
                    | FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS)
IO_REPARSE_TAG_MOUNT_POINT = 0xA0000003

# Folders skipped by default. Windows names apply at a drive's root;
# ANYWHERE_SYSTEM names are skipped at any depth.
WINDOWS_ROOT_SYSTEM = {
    "windows", "program files", "program files (x86)", "programdata",
    "$recycle.bin", "system volume information", "recovery",
    "windows.old", "$windows.~bt", "$windows.~ws", "$winreagent", "$sysreset",
    "perflogs", "msocache", "config.msi", "onedrivetemp",
}
PROGRAM_FILE_EXTS = (".dll",)   # a folder holding these belongs to a program
ANYWHERE_SYSTEM = {".git", "node_modules", "venv", ".venv", "site-packages",
                   # Game and app libraries keep program files outside Program Files.
                   "steamapps", "steamlibrary", "epic games", "xboxgames", "windowsapps",
                   "gog games", "riot games", "ea games", "ubisoft game launcher",
                   "battle.net", "__pycache__"}
POSIX_SYSTEM = {
    "/proc", "/sys", "/dev", "/run", "/boot", "/usr", "/bin", "/sbin", "/lib",
    "/lib32", "/lib64", "/libx32", "/etc", "/var", "/snap", "/opt", "/srv",
    "/lost+found", "/System", "/Library", "/Applications", "/private",
}
POSIX_TRASH_NAMES = {".trash", ".trashes"}

COPY_MARKER_RE = re.compile(
    r"(\(\d+\)$|\s*-\s*copy(\s*\(\d+\))?$|^copy of\s|[\s_]copy\d*$|\(copy\)|"
    r"\s*-\s*kopie$|\s*-\s*copie$)", re.IGNORECASE)

log = logging.getLogger("dupefinder")


# --------------------------------------------------------------------------
# Network guard: this tool never talks to anything but this computer.
# --------------------------------------------------------------------------

def _is_loopback_host(host) -> bool:
    if host is None:
        return True
    if isinstance(host, bytes):
        host = host.decode("ascii", "replace")
    host = str(host).strip("[]").split("%")[0]
    if host in ("", "localhost"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def install_network_guard() -> None:
    """Refuse every connection that does not go to this computer.

    Applied in the main process and in every worker, so no library can reach
    the internet either (no downloads, no telemetry, no update checks).
    """
    if getattr(socket, "_dupefinder_guard", False):
        return
    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex
    real_getaddrinfo = socket.getaddrinfo

    def check(sock, address):
        if sock.family in (socket.AF_INET, socket.AF_INET6):
            host = address[0] if isinstance(address, tuple) else address
            if not _is_loopback_host(host):
                raise PermissionError(
                    f"{APP_NAME} works offline and blocked a connection to {host}")

    def connect(self, address):
        check(self, address)
        return real_connect(self, address)

    def connect_ex(self, address):
        check(self, address)
        return real_connect_ex(self, address)

    def getaddrinfo(host, *args, **kwargs):
        if not _is_loopback_host(host):
            raise socket.gaierror(
                f"{APP_NAME} works offline and blocked a lookup of {host}")
        return real_getaddrinfo(host, *args, **kwargs)

    def guard_lookup(name):
        real = getattr(socket, name)

        def lookup(host, *args, **kwargs):
            target = host[0] if isinstance(host, tuple) else host
            if not _is_loopback_host(target):
                raise socket.gaierror(f"{APP_NAME} works offline and blocked a lookup of {target}")
            return real(host, *args, **kwargs)
        setattr(socket, name, lookup)

    real_sendto = socket.socket.sendto

    def sendto(self, data, *args):
        check(self, args[-1])
        return real_sendto(self, data, *args)

    socket.socket.connect = connect
    socket.socket.connect_ex = connect_ex
    socket.socket.sendto = sendto
    socket.getaddrinfo = getaddrinfo
    for name in ("gethostbyname", "gethostbyname_ex", "gethostbyaddr", "getnameinfo"):
        guard_lookup(name)
    socket._dupefinder_guard = True
    # A local proxy (system or environment setting) must not relay anything out.
    os.environ["NO_PROXY"] = os.environ["no_proxy"] = "*"
    import urllib.request
    urllib.request.getproxies = lambda: {}
    # Belt and braces for optional AI libraries: never fetch model weights.
    for var in ("HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE", "HF_DATASETS_OFFLINE"):
        os.environ.setdefault(var, "1")


# --------------------------------------------------------------------------
# Paths
# --------------------------------------------------------------------------

def to_extended_path(path: str, windows: bool = IS_WINDOWS) -> str:
    r"""Return the \\?\ form of an absolute Windows path (no-op elsewhere).

    Lets Windows open paths longer than 260 characters.
    """
    if not windows or path.startswith("\\\\?\\"):
        return path
    if path.startswith("\\\\"):
        return "\\\\?\\UNC\\" + path[2:]
    return "\\\\?\\" + path


def io_path(path: str) -> str:
    return to_extended_path(os.path.abspath(path)) if IS_WINDOWS else path


def path_key(path: str) -> str:
    """Comparable form of a path (case-insensitive on Windows)."""
    p = os.path.normcase(os.path.abspath(path))
    return p.rstrip("\\/") or p


def is_under(path: str, base_key: str) -> bool:
    key = path_key(path)
    if key == base_key:
        return True
    base = base_key if base_key.endswith(os.sep) else base_key + os.sep
    return key.startswith(base)


def is_drive_root(path: str) -> bool:
    drive, rest = os.path.splitdrive(os.path.abspath(path))
    return rest.strip("\\/") == ""


def default_cache_dir() -> str:
    if IS_WINDOWS:
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~\\AppData\\Local")
    elif IS_MAC:
        base = os.path.expanduser("~/Library/Caches")
    else:
        base = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
    return os.path.join(base, APP_NAME)


def has_copy_marker(path: str) -> bool:
    stem = os.path.splitext(os.path.basename(path))[0]
    return bool(COPY_MARKER_RE.search(stem))


def human_size(n: float) -> str:
    for unit in ("bytes", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "bytes" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def file_uri(path: str) -> str:
    try:
        return Path(path).as_uri()
    except ValueError:
        return ""


def signed64(v: int | None) -> int | None:
    if v is None:
        return None
    return v - (1 << 64) if v >= (1 << 63) else v


def unsigned64(v: int | None) -> int | None:
    if v is None:
        return None
    return v + (1 << 64) if v < 0 else v


def popcount64(v: int) -> int:
    return bin(v).count("1")


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(io_path(path), "rb") as f:
        while True:
            chunk = f.read(READ_CHUNK)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


# --------------------------------------------------------------------------
# Optional and required dependencies
# --------------------------------------------------------------------------

_pymupdf = None
_heif_ok = None


def get_pymupdf():
    """Import PyMuPDF lazily (only PDF scans need it)."""
    global _pymupdf
    if _pymupdf is None:
        try:
            import pymupdf as mod
        except ImportError:
            import fitz as mod  # PyMuPDF before 1.24
        try:
            mod.TOOLS.mupdf_display_errors(False)
            mod.TOOLS.mupdf_display_warnings(False)
        except Exception:
            pass
        _pymupdf = mod
    return _pymupdf


def enable_heif() -> bool:
    global _heif_ok
    if _heif_ok is None:
        try:
            import pillow_heif
            pillow_heif.register_heif_opener()
            _heif_ok = True
        except Exception:
            _heif_ok = False
    return _heif_ok


def check_dependencies(types: set[str], need_trash: bool) -> list[str]:
    """Return problems that stop the run; warn about optional ones."""
    missing = []
    if np is None:
        missing.append("numpy")
    if Image is None:
        missing.append("Pillow")
    if "pdf" in types:
        try:
            get_pymupdf()
        except ImportError:
            missing.append("PyMuPDF")
    if need_trash:
        import importlib.util
        if importlib.util.find_spec("send2trash") is None:
            missing.append("Send2Trash")
    if "image" in types and Image is not None and not enable_heif():
        print("Note: pillow-heif is not installed, so HEIC/HEIF photos are skipped.")
    return missing


# --------------------------------------------------------------------------
# Data model
# --------------------------------------------------------------------------

@dataclass(slots=True)
class FileRec:
    fid: int
    path: str
    kind: str                      # "pdf" or "image"
    size: int
    mtime_ns: int
    dev: int = 0
    ino: int = 0
    nlink: int = 1
    system: bool = False
    file_sha: str | None = None
    content_sha: str | None = None  # normalized-text (PDF) or pixel (photo) fingerprint
    minhash: object = None          # numpy uint64[128] or None
    phash: int | None = None
    page_hashes: list | None = None
    taken_at: float | None = None
    camera: str | None = None
    sharpness: float | None = None
    exposure: dict | None = None
    embedding: object = None
    meta: dict = field(default_factory=dict)
    error: str | None = None
    analysed: bool = False          # analysis fields are valid (fresh or cached)

    @property
    def name(self) -> str:
        return os.path.basename(self.path)

    @property
    def pixels(self) -> int:
        return int(self.meta.get("w", 0)) * int(self.meta.get("h", 0))

    @property
    def flat(self) -> bool:
        return bool(self.meta.get("flat"))


@dataclass
class ScanStats:
    started: float = field(default_factory=time.time)
    finished: float = 0.0
    files_seen: int = 0
    system_skipped: int = 0
    program_skipped: int = 0
    excluded: int = 0
    links: int = 0
    online_only: int = 0
    empty: int = 0
    same_file: int = 0
    denied: list = field(default_factory=list)       # [(path, reason)]
    errors: list = field(default_factory=list)       # [(path, error)]
    protected: list = field(default_factory=list)    # [path]
    analysed: int = 0
    from_cache: int = 0
    interrupted: bool = False

    def counts(self) -> dict:
        c = {
            "System or program folders": self.system_skipped,
            "Other program folders (they contain .dll files)": self.program_skipped,
            "Excluded folders (--exclude)": self.excluded,
            "Links and junctions not followed": self.links,
            "Online-only (OneDrive) files, not downloaded": self.online_only,
            "Same file at two paths (hard links)": self.same_file,
            "Empty files": self.empty,
            "Folders that couldn't be opened": len(self.denied),
            "Files that couldn't be read": len(self.errors),
            "Password-protected PDFs (exact copies only)": len(self.protected),
        }
        return {k: v for k, v in c.items() if v}


# --------------------------------------------------------------------------
# Scanner: walking folders, exclusions, links and OneDrive placeholders
# --------------------------------------------------------------------------

def file_attributes(entry, st) -> int:
    """Windows file attributes (0 elsewhere). Tests replace this function."""
    return getattr(st, "st_file_attributes", 0) or 0


def entry_is_junction(entry) -> bool:
    fn = getattr(entry, "is_junction", None)   # Python 3.12+
    if fn is not None:
        return fn()
    if IS_WINDOWS:
        try:
            st = entry.stat(follow_symlinks=False)
            return getattr(st, "st_reparse_tag", 0) == IO_REPARSE_TAG_MOUNT_POINT
        except OSError:
            return False
    return False


def system_folder_reason(path: str, name: str, parent: str) -> str | None:
    n = name.casefold()
    if n in ANYWHERE_SYSTEM:
        return name
    if IS_WINDOWS:
        if n in WINDOWS_ROOT_SYSTEM and is_drive_root(parent):
            return name
        if n == "appdata" and os.path.basename(os.path.dirname(parent)).casefold() == "users":
            return "AppData"
    else:
        if path in POSIX_SYSTEM:
            return path
        if n in POSIX_TRASH_NAMES or n.startswith(".trash-"):
            return name
        home = os.path.expanduser("~")
        if path in (os.path.join(home, ".local", "share", "Trash"),
                    os.path.join(home, "Library")):
            return path
    return None


def path_in_system_folder(path: str) -> bool:
    path = os.path.abspath(path)
    parent = os.path.dirname(path)
    while parent and parent != path:
        if system_folder_reason(path, os.path.basename(path), parent):
            return True
        path, parent = parent, os.path.dirname(parent)
    return False


class Walker:
    """Walk roots with os.scandir, pruning skipped folders before descending."""

    def __init__(self, opts, stats: ScanStats, own_dirs: list[str]):
        self.opts = opts
        self.stats = stats
        self.types = opts.type_set
        self.exclude_keys = [path_key(p) for p in opts.exclude]
        self.own_keys = [path_key(p) for p in own_dirs]
        self.seen_ids: set = set()
        self.listed: set = set()      # folders read completely this run

    def walk(self, roots: list[str]):
        for root in roots:
            yield from self._walk_root(root)

    def _walk_root(self, root: str):
        # A root inside a system folder is scanned (the user asked for it),
        # but its files are still never pre-selected for trash.
        stack = [(root, path_in_system_folder(root))]
        while stack:
            folder, in_system = stack.pop()
            try:
                it = os.scandir(io_path(folder))
            except PermissionError as e:
                self.stats.denied.append((folder, "Permission denied"))
                log.info("Skipped %s: %s", folder, e)
                continue
            except OSError as e:
                self.stats.denied.append((folder, e.strerror or str(e)))
                log.info("Skipped %s: %s", folder, e)
                continue
            entries = []
            complete = True
            with it:
                while True:
                    try:
                        entries.append(next(it))
                    except StopIteration:
                        break
                    except OSError as e:
                        self.stats.denied.append((folder, e.strerror or str(e)))
                        complete = False
                        break
            if any(e.name.lower().endswith(PROGRAM_FILE_EXTS) for e in entries):
                # Program files live here (e.g. a game, a portable app, a Python
                # install): their images must stay put, whatever the folder's name.
                if folder != root and not self.opts.include_system:
                    self.stats.program_skipped += 1
                    continue
                in_system = True
            subdirs = []
            for entry in entries:
                rec = self._visit(folder, entry, in_system, subdirs)
                if rec is not None:
                    yield rec
            if complete:
                self.listed.add(path_key(folder))
            # Reverse so folders are visited in name order.
            stack.extend(reversed(sorted(subdirs)))

    def _visit(self, folder, entry, in_system, subdirs):
        name = entry.name
        full = os.path.join(folder, name)
        try:
            full.encode("utf-8")
        except UnicodeEncodeError:
            self.stats.errors.append((full.encode("utf-8", "replace").decode("utf-8"),
                                      "Name can't be read as text; skipped"))
            return None
        try:
            if entry.is_symlink() or entry_is_junction(entry):
                self.stats.links += 1
                return None
            is_dir = entry.is_dir(follow_symlinks=False)
        except OSError:
            return None
        if is_dir:
            if name.casefold() == REVIEW_DIR or any(is_under(full, k) for k in self.own_keys):
                return None
            if any(is_under(full, k) for k in self.exclude_keys):
                self.stats.excluded += 1
                return None
            reason = system_folder_reason(full, name, folder)
            if reason and not self.opts.include_system:
                self.stats.system_skipped += 1
                return None
            subdirs.append((full, in_system or bool(reason)))
            return None
        ext = os.path.splitext(name)[1].lower()
        kind = "pdf" if ext in PDF_EXTS else "image" if ext in IMAGE_EXTS else None
        if kind is None or kind not in self.types:
            return None
        if kind == "image" and ext in HEIF_EXTS and not enable_heif():
            return None
        try:
            st = entry.stat(follow_symlinks=False)
            if file_attributes(entry, st) & ONLINE_ONLY_MASK:
                self.stats.online_only += 1
                return None
            if st.st_size == 0:
                self.stats.empty += 1
                return None
            if IS_WINDOWS or not st.st_ino:
                # DirEntry.stat() has no file ID or link count on Windows.
                st = os.stat(io_path(full), follow_symlinks=False)
        except OSError as e:
            self.stats.errors.append((full, f"Couldn't read file details: {e.strerror or e}"))
            return None
        key = (st.st_dev, st.st_ino)
        if st.st_ino:
            if key in self.seen_ids:
                self.stats.same_file += 1
                return None
            self.seen_ids.add(key)
        self.stats.files_seen += 1
        return FileRec(fid=-1, path=full, kind=kind, size=st.st_size,
                       mtime_ns=st.st_mtime_ns, dev=st.st_dev, ino=st.st_ino,
                       nlink=st.st_nlink, system=in_system)


def compact_meta(meta: dict) -> dict:
    """Keep per-file memory small on huge scans (tile values as float32)."""
    tiles = meta.get("tiles")
    if isinstance(tiles, list):
        meta["tiles"] = np.asarray(tiles, dtype=np.float32)
    return meta


def _json_default(v):
    if isinstance(v, np.ndarray):
        return [round(float(x), 2) for x in v]
    if isinstance(v, np.generic):
        return v.item()
    raise TypeError(f"not JSON serializable: {type(v)}")


# --------------------------------------------------------------------------
# Scan cache (SQLite)
# --------------------------------------------------------------------------

CACHE_SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
    path TEXT PRIMARY KEY,
    volume_id TEXT, file_id TEXT,
    size INTEGER, mtime_ns INTEGER,
    kind TEXT,
    file_sha256 TEXT,
    content_sha256 TEXT,
    minhash BLOB,
    phash INTEGER,
    page_hashes BLOB,
    taken_at REAL, camera TEXT,
    sharpness REAL, exposure TEXT,
    embedding BLOB,
    meta TEXT,
    error TEXT,
    algo_version INTEGER,
    last_scan INTEGER
);
CREATE TABLE IF NOT EXISTS scans (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started TEXT, finished TEXT, roots TEXT, options TEXT, counts TEXT
);
CREATE TABLE IF NOT EXISTS actions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    time TEXT, scan_id INTEGER, path TEXT, action TEXT, destination TEXT,
    group_id INTEGER, kept_path TEXT, file_sha256 TEXT, size INTEGER, note TEXT
);
"""


class Cache:
    """Fingerprints keyed by path, so later scans only process new or changed files."""

    def __init__(self, cache_dir: str):
        self.dir = cache_dir
        os.makedirs(cache_dir, exist_ok=True)
        self.path = os.path.join(cache_dir, "cache.sqlite")
        self.lock = threading.RLock()
        self.db = sqlite3.connect(self.path, check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.executescript(CACHE_SCHEMA)
        self.db.commit()

    def close(self):
        with self.lock:
            self.db.close()

    # -- files ---------------------------------------------------------
    def lookup(self, paths: list[str]) -> dict:
        out = {}
        with self.lock:
            cur = self.db.cursor()
            for i in range(0, len(paths), 500):
                chunk = paths[i:i + 500]
                q = "SELECT * FROM files WHERE path IN (%s)" % ",".join("?" * len(chunk))
                cur.execute(q, chunk)
                cols = [d[0] for d in cur.description]
                for row in cur.fetchall():
                    r = dict(zip(cols, row))
                    out[r["path"]] = r
        return out

    @staticmethod
    def row_from_rec(rec: FileRec, scan_id: int) -> tuple:
        return (
            rec.path, str(rec.dev), str(rec.ino), rec.size, rec.mtime_ns, rec.kind,
            rec.file_sha, rec.content_sha,
            None if rec.minhash is None else np.asarray(rec.minhash, dtype=np.uint64).tobytes(),
            signed64(rec.phash),
            None if rec.page_hashes is None else np.asarray(rec.page_hashes, dtype=np.uint64).tobytes(),
            rec.taken_at, rec.camera, rec.sharpness,
            None if rec.exposure is None else json.dumps(rec.exposure),
            None if rec.embedding is None else np.asarray(rec.embedding, dtype=np.float16).tobytes(),
            json.dumps(rec.meta, default=_json_default), rec.error, ALGO_VERSION, scan_id,
        )

    def save(self, recs: list[FileRec], scan_id: int):
        if not recs:
            return
        rows = [self.row_from_rec(r, scan_id) for r in recs]
        with self.lock:
            self.db.executemany(
                "INSERT OR REPLACE INTO files VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
            self.db.commit()

    def touch(self, paths: list[str], scan_id: int):
        with self.lock:
            self.db.executemany("UPDATE files SET last_scan=? WHERE path=?",
                                [(scan_id, p) for p in paths])
            self.db.commit()

    def forget_missing(self, listed: set, kinds: set[str], scan_id: int) -> int:
        """Delete rows for files not found during this full scan of their folder.

        `listed` holds the path keys of folders read completely this run, so
        excluded, skipped or unreadable folders keep their cached rows.
        """
        with self.lock:
            rows = self.db.execute(
                "SELECT path, kind FROM files WHERE last_scan IS NULL OR last_scan != ?",
                (scan_id,)).fetchall()
            gone = [p for p, k in rows
                    if k in kinds and path_key(os.path.dirname(p)) in listed]
            self.db.executemany("DELETE FROM files WHERE path=?", [(p,) for p in gone])
            self.db.commit()
        return len(gone)

    @staticmethod
    def apply_row(rec: FileRec, row: dict, analysis: bool):
        rec.file_sha = row["file_sha256"]
        if not analysis:
            return
        rec.content_sha = row["content_sha256"]
        rec.minhash = None if row["minhash"] is None else np.frombuffer(row["minhash"], dtype=np.uint64)
        rec.phash = unsigned64(row["phash"])
        rec.page_hashes = (None if row["page_hashes"] is None
                           else [int(x) for x in np.frombuffer(row["page_hashes"], dtype=np.uint64)])
        rec.taken_at = row["taken_at"]
        rec.camera = row["camera"]
        rec.sharpness = row["sharpness"]
        rec.exposure = json.loads(row["exposure"]) if row["exposure"] else None
        rec.embedding = (None if row["embedding"] is None
                         else np.frombuffer(row["embedding"], dtype=np.float16).astype(np.float32))
        rec.meta = compact_meta(json.loads(row["meta"]) if row["meta"] else {})
        rec.error = row["error"]
        rec.analysed = True

    # -- scans and actions --------------------------------------------
    def start_scan(self, roots, options) -> int:
        with self.lock:
            cur = self.db.execute(
                "INSERT INTO scans (started, roots, options) VALUES (?,?,?)",
                (dt.datetime.now().isoformat(timespec="seconds"), json.dumps(roots),
                 json.dumps(options, default=str)))
            self.db.commit()
            return cur.lastrowid

    def finish_scan(self, scan_id: int, counts: dict):
        with self.lock:
            self.db.execute("UPDATE scans SET finished=?, counts=? WHERE id=?",
                            (dt.datetime.now().isoformat(timespec="seconds"),
                             json.dumps(counts), scan_id))
            self.db.commit()

    def log_action(self, scan_id, path, action, destination, group_id, kept_path,
                   file_sha, size, note=""):
        with self.lock:
            cur = self.db.execute(
                "INSERT INTO actions (time, scan_id, path, action, destination, group_id, "
                "kept_path, file_sha256, size, note) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (dt.datetime.now().isoformat(timespec="seconds"), scan_id, path, action,
                 destination, group_id, kept_path, file_sha, size, note))
            self.db.commit()
            return cur.lastrowid

    def mark_failed(self, action_id: int, why: str):
        with self.lock:
            self.db.execute("UPDATE actions SET action='failed', note=? WHERE id=?",
                            (why, action_id))
            self.db.commit()

    def actions(self) -> list[dict]:
        with self.lock:
            cur = self.db.execute("SELECT * FROM actions ORDER BY id")
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, r)) for r in cur.fetchall()]

    def export_actions(self) -> str:
        path = os.path.join(self.dir, "actions.csv")
        rows = self.actions()
        cols = ["id", "time", "scan_id", "path", "action", "destination", "group_id",
                "kept_path", "file_sha256", "size", "note"]
        tmp = path + ".tmp"
        with open(tmp, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=cols)
            w.writeheader()
            for r in rows:
                w.writerow({c: r.get(c) for c in cols})
        os.replace(tmp, path)
        return path


# --------------------------------------------------------------------------
# Fingerprint maths: perceptual hash, shingles and MinHash (NumPy only)
# --------------------------------------------------------------------------

_DCT32 = None
U64 = None if np is None else np.uint64


def _dct_matrix():
    global _DCT32
    if _DCT32 is None:
        n = 32
        k = np.arange(n)[:, None]
        m = np.arange(n)[None, :]
        _DCT32 = np.cos(np.pi * k * (2 * m + 1) / (2 * n))
    return _DCT32


def phash_gray(gray) -> tuple[int, float]:
    """64-bit perceptual hash (DCT of a 32x32 grey copy, 8x8 block vs median).

    Same algorithm as ImageHash's phash. Also returns the grey standard
    deviation of the 32x32 copy, used to spot flat (featureless) images.
    """
    h, std, _ = phash_full(gray)
    return h, std


_SIGN8 = None


def _bits64(block) -> int:
    return int.from_bytes(np.packbits((block > np.median(block)).ravel()).tobytes(), "big")


def phash_full(gray):
    """pHash plus the hashes of the same image turned 90/180/270 degrees and mirrored.

    A flip or 90-degree turn of an image only changes signs and swaps axes in
    its DCT, so all variants come from one transform (no extra decoding).
    """
    global _SIGN8
    if _SIGN8 is None:
        _SIGN8 = (-1.0) ** np.arange(8)
    small = gray.resize((32, 32), Image.Resampling.LANCZOS)
    x = np.asarray(small, dtype=np.float64)
    c = _dct_matrix()
    low = (c @ x @ c.T)[:8, :8]
    sr, sc = _SIGN8[:, None], _SIGN8[None, :]
    variants = [_bits64(low.T * sc), _bits64(low * sr * sc), _bits64(low.T * sr), _bits64(low * sc)]
    return _bits64(low), float(x.std()), variants


def hash_is_flat(h: int, std: float) -> bool:
    ones = popcount64(h)
    return std < FLAT_STD or ones < 8 or ones > 56


def mix64(z):
    """splitmix64 finaliser on a uint64 array (wraps on overflow by design)."""
    z = z ^ (z >> U64(30))
    z = z * U64(0xBF58476D1CE4E5B9)
    z = z ^ (z >> U64(27))
    z = z * U64(0x94D049BB133111EB)
    return z ^ (z >> U64(31))


_MH_SEEDS = None


def minhash_seeds():
    global _MH_SEEDS
    if _MH_SEEDS is None:
        with np.errstate(over="ignore"):
            _MH_SEEDS = mix64(np.arange(1, MINHASH_PERM + 1, dtype=np.uint64)
                              * U64(0x9E3779B97F4A7C15))
    return _MH_SEEDS


def token_hashes(tokens: list[str]):
    cache: dict = {}
    out = np.empty(len(tokens), dtype=np.uint64)
    for i, t in enumerate(tokens):
        v = cache.get(t)
        if v is None:
            v = int.from_bytes(hashlib.blake2b(t.encode("utf-8"), digest_size=8).digest(), "little")
            cache[t] = v
        out[i] = v
    return out


def shingle_set(tokens: list[str]):
    """Sorted unique 64-bit hashes of every run of 5 consecutive words."""
    if not tokens:
        return np.empty(0, dtype=np.uint64)
    th = token_hashes(tokens)
    with np.errstate(over="ignore"):
        if len(th) < SHINGLE_WORDS:
            acc = np.zeros(1, dtype=np.uint64)
            for v in th:
                acc = acc * U64(0x100000001B3) + v
        else:
            n = len(th) - SHINGLE_WORDS + 1
            acc = np.zeros(n, dtype=np.uint64)
            for k in range(SHINGLE_WORDS):
                acc = acc * U64(0x100000001B3) + th[k:k + n]
        return np.unique(mix64(acc))


def minhash_signature(shingles):
    seeds = minhash_seeds()
    sig = np.full(MINHASH_PERM, np.iinfo(np.uint64).max, dtype=np.uint64)
    with np.errstate(over="ignore"):
        for i in range(0, len(shingles), 4096):
            block = shingles[i:i + 4096]
            v = mix64(block[None, :] ^ seeds[:, None])
            sig = np.minimum(sig, v.min(axis=1))
    return sig


def exact_jaccard(a, b) -> float:
    if len(a) == 0 or len(b) == 0:
        return 0.0
    inter = len(np.intersect1d(a, b, assume_unique=True))
    return inter / (len(a) + len(b) - inter)


def lsh_params(target: float) -> tuple[int, int]:
    """Bands and rows so a pair at `target` overlap is found with >= 98% recall."""
    best = (MINHASH_PERM, 1)
    for r in range(1, MINHASH_PERM + 1):
        b = MINHASH_PERM // r
        if b < 1:
            break
        if 1 - (1 - target ** r) ** b >= LSH_RECALL:
            best = (b, r)
    return best


# --------------------------------------------------------------------------
# Text normalization (PDFs)
# --------------------------------------------------------------------------

PAGE_NUMBER_RE = re.compile(
    r"^(?:page|p\.|seite|página|pagina)?\s*(?:\d+|[ivxlc]{1,6})(?:\s*(?:of|/|von|de)\s*\d+)?$")
WORD_RE = re.compile(r"\w+")


def normalize_pages(pages: list[str]) -> str:
    """NFKC, lowercase, drop repeated headers/footers and bare page numbers,
    rejoin words hyphenated across line breaks, and collapse whitespace."""
    per_page = []
    for text in pages:
        text = unicodedata.normalize("NFKC", text or "").lower()
        lines = [re.sub(r"\s+", " ", ln).strip() for ln in text.splitlines()]
        per_page.append([ln for ln in lines if ln])

    def key(line):
        return re.sub(r"\d+", "#", line)

    drop = set()
    if len(per_page) >= 3:
        counts = collections.Counter()
        for lines in per_page:
            counts.update({key(ln) for ln in lines})
        drop = {k for k, c in counts.items() if c > len(per_page) / 2}
    kept = [ln for lines in per_page for ln in lines
            if key(ln) not in drop and not PAGE_NUMBER_RE.match(ln)]
    text = "\n".join(kept)
    text = re.sub(r"(\w)-\n(\w)", r"\1\2", text)
    return re.sub(r"\s+", " ", text).strip()


# --------------------------------------------------------------------------
# Photos: decoding, EXIF, quality
# --------------------------------------------------------------------------

class _FileWindow(io.RawIOBase):
    """Read-only view of a file starting at `offset` (for embedded images)."""

    def __init__(self, f, offset: int):
        self.f, self.offset = f, offset
        self.f.seek(offset)

    def readable(self):
        return True

    def seekable(self):
        return True

    def seek(self, pos, whence=0):
        if whence == 0:
            pos += self.offset
        return self.f.seek(pos, whence) - self.offset

    def tell(self):
        return self.f.tell() - self.offset

    def readinto(self, b):
        data = self.f.read(len(b))
        b[:len(data)] = data
        return len(data)


TRANSPOSE_FOR_ORIENTATION = {2: "FLIP_LEFT_RIGHT", 3: "ROTATE_180", 4: "FLIP_TOP_BOTTOM",
                             5: "TRANSPOSE", 6: "ROTATE_270", 7: "TRANSVERSE", 8: "ROTATE_90"}


def load_raw(path: str, max_side: int | None):
    """Camera RAW: decode the largest embedded JPEG preview (no RAW decoder needed)."""
    import mmap
    with open(io_path(path), "rb") as f, mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as mm:
        best, pos, tried = None, 0, 0
        while tried < 60:
            off = mm.find(b"\xff\xd8\xff", pos)
            if off < 0:
                break
            pos = off + 3
            if off + 3 >= len(mm):
                break
            if mm[off + 3] not in (0xDB, 0xC0, 0xC4) and not 0xE0 <= mm[off + 3] <= 0xEF:
                continue
            tried += 1
            try:
                im = Image.open(_FileWindow(f, off))
                if im.format == "JPEG" and (best is None or im.width * im.height > best[1]):
                    best = (off, im.width * im.height)
            except Exception:
                continue
        if best is None or best[1] < 160 * 120:
            raise ValueError("no usable preview image inside this RAW file")
        im = Image.open(_FileWindow(f, best[0]))
        exif = im.getexif()
        if max_side:
            im.draft("RGB", (max_side, max_side))
        im.load()
    if not exif or not exif.get(0x0110):
        try:                        # TIFF-based RAW: the container holds the EXIF
            with open(io_path(path), "rb") as f:
                exif = Image.open(f).getexif()
        except Exception:
            pass
    orientation = int((exif or {}).get(0x0112, 1) or 1)
    if orientation in TRANSPOSE_FOR_ORIENTATION:
        im = im.transpose(getattr(Image.Transpose, TRANSPOSE_FOR_ORIENTATION[orientation]))
    w, h = im.size
    if max_side and max(im.size) > max_side:
        im.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
    return im, {"format": "RAW", "w": w, "h": h, "exif": exif}


def is_raw(path: str) -> bool:
    return os.path.splitext(path)[1].lower() in RAW_EXTS


def load_photo(path: str, max_side: int | None):
    """Decode a photo with its rotation applied, reduced to max_side if given.

    Returns (image, info) where info has the full-size rotated width/height.
    """
    if is_raw(path):
        return load_raw(path, max_side)
    with open(io_path(path), "rb") as f:
        im = Image.open(f)
        fmt = im.format
        w0, h0 = im.size
        try:
            exif = im.getexif()
        except Exception:
            exif = None
        if max_side and fmt == "JPEG":
            im.draft("RGB", (max_side, max_side))
        im.load()
    orientation = 1
    if exif:
        try:
            orientation = int(exif.get(0x0112, 1) or 1)
        except (TypeError, ValueError):
            orientation = 1
    try:
        im = ImageOps.exif_transpose(im)
    except Exception:
        pass
    if max_side and max(im.size) > max_side:
        im.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
    w, h = (h0, w0) if orientation in (5, 6, 7, 8) else (w0, h0)
    return im, {"format": fmt, "w": w, "h": h, "exif": exif}


def to_gray(im):
    if im.mode.startswith("I") or im.mode == "F":
        a = np.asarray(im, dtype=np.float64)
        top = a.max() or 1.0
        scale = 255.0 / (65535.0 if top > 255 else 255.0)
        return Image.fromarray(np.clip(a * scale, 0, 255).astype(np.uint8), "L")
    return im.convert("L")


def _clean_exif_text(v) -> str:
    if v is None:
        return ""
    if isinstance(v, bytes):
        v = v.decode("utf-8", "replace")
    return str(v).replace("\x00", "").strip()


def exif_capture(exif) -> tuple[float | None, str, str]:
    """(capture time as seconds, display string, camera) from EXIF."""
    if not exif:
        return None, "", ""
    make = _clean_exif_text(exif.get(0x010F))
    model = _clean_exif_text(exif.get(0x0110))
    camera = model if make and model.lower().startswith(make.lower()) else f"{make} {model}".strip()
    try:
        sub = exif.get_ifd(0x8769)
    except Exception:
        sub = {}
    raw = _clean_exif_text(sub.get(0x9003) or exif.get(0x9003))
    m = re.match(r"(\d{4})[:\-](\d{2})[:\-](\d{2})[ T](\d{2}):(\d{2}):(\d{2})", raw)
    if not m:
        return None, "", camera
    try:
        stamp = dt.datetime(*map(int, m.groups()))
    except ValueError:
        return None, "", camera
    seconds = float(stamp.replace(tzinfo=dt.timezone.utc).timestamp())
    subsec = re.sub(r"\D", "", _clean_exif_text(sub.get(0x9291)))
    if subsec:
        seconds += float("0." + subsec)
    shown = stamp.strftime("%Y-%m-%d %H:%M:%S") + (f".{subsec[:3]}" if subsec else "")
    return seconds, shown, camera


def sharpness_tiles(a) -> tuple[float, list[float]]:
    """Variance of the Laplacian per tile of a 4x4 grid; score = mean of the 4 sharpest."""
    if a.shape[0] < 12 or a.shape[1] < 12:
        return 0.0, []
    lap = (a[1:-1, :-2] + a[1:-1, 2:] + a[:-2, 1:-1] + a[2:, 1:-1]) - 4.0 * a[1:-1, 1:-1]
    hs, ws = lap.shape[0] // 4, lap.shape[1] // 4
    tiles = [float(lap[i * hs:(i + 1) * hs, j * ws:(j + 1) * ws].var())
             for i in range(4) for j in range(4)]
    return float(np.mean(sorted(tiles)[-4:])), tiles


def noise_sigma(a) -> float:
    """Immerkaer's fast estimate of the image noise standard deviation."""
    if a.shape[0] < 3 or a.shape[1] < 3:
        return 0.0
    m = (a[:-2, :-2] - 2 * a[:-2, 1:-1] + a[:-2, 2:] - 2 * a[1:-1, :-2] + 4 * a[1:-1, 1:-1]
         - 2 * a[1:-1, 2:] + a[2:, :-2] - 2 * a[2:, 1:-1] + a[2:, 2:])
    return float(np.sqrt(np.pi / 2) * np.abs(m).sum() / (6 * (a.shape[1] - 2) * (a.shape[0] - 2)))


def _box_blur(a, n: int, axis: int):
    pad = [(n // 2, n // 2) if ax == axis else (0, 0) for ax in range(2)]
    c = np.cumsum(np.pad(a, pad, mode="edge"), axis=axis, dtype=np.float64)
    c = np.insert(c, 0, 0.0, axis=axis)
    hi = np.take(c, np.arange(n, c.shape[axis]), axis=axis)
    lo = np.take(c, np.arange(0, c.shape[axis] - n), axis=axis)
    return (hi - lo) / n


def reblur_blur(a) -> float:
    """Crete et al. (2007) no-reference blur: 0 = sharp ... 1 = blurry.

    Re-blurring a sharp image removes a lot of its variation; re-blurring a
    blurry one changes little. Measured both ways, so motion blur counts too.
    """
    worst = 0.0
    for axis in (0, 1):
        if a.shape[axis] < 12:
            continue
        df_ = np.abs(np.diff(a, axis=axis))
        db = np.abs(np.diff(_box_blur(a, 9, axis), axis=axis))
        s_f = df_.sum()
        if s_f <= 0:
            return 1.0
        worst = max(worst, float((s_f - np.maximum(0, df_ - db).sum()) / s_f))
    return worst


def blur_measures(gray):
    """(score 0-100, detail sharpness, tiles).

    The score is the re-blur measure on the 4 sharpest of 16 tiles of a
    noise-filtered copy: noise looks like fine detail, so it is filtered first,
    by an amount that depends on the measured noise level. Using the sharpest
    tiles means a sharp subject on a soft background still scores well, and
    the measure ignores darkness and low contrast. The detail sharpness
    (Laplacian variance of the 4 sharpest tiles) ranks the frames of a burst.
    """
    from PIL import ImageFilter
    raw = np.asarray(gray, dtype=np.float32)
    # Frames of one burst share their noise, so they are ranked on the raw image.
    lap, raw_tiles = sharpness_tiles(raw)
    if not raw_tiles:
        return 0.0, 0.0, []
    sig = noise_sigma(raw)
    if sig > 6:
        gray = gray.filter(ImageFilter.MedianFilter(5))
    elif sig > 2.5:
        gray = gray.filter(ImageFilter.MedianFilter(3))
    a = np.asarray(gray, dtype=np.float32)
    _, tiles = sharpness_tiles(a)
    hs, ws = (a.shape[0] - 2) // 4, (a.shape[1] - 2) // 4
    top = np.argsort(tiles)[-4:]
    blur = float(np.mean([reblur_blur(a[(t // 4) * hs:(t // 4 + 1) * hs + 2,
                                        (t % 4) * ws:(t % 4 + 1) * ws + 2]) for t in top]))
    return round((1 - blur) * 100, 1), round(lap, 2), raw_tiles


def chroma_signature(im) -> list[float]:
    """Average colour (YCbCr chroma) and how colourful the image is."""
    ycc = np.asarray(_rgb_for_jpeg(im).convert("YCbCr"), dtype=np.float32)
    cb, cr = ycc[..., 1], ycc[..., 2]
    sat = float(np.mean(np.abs(cb - 128) + np.abs(cr - 128)))
    return [round(float(cb.mean()), 1), round(float(cr.mean()), 1), round(sat, 1)]


def centre_crop_hash(gray) -> int:
    """pHash of the centre square (wide/tall photos) or the central 80% (squarish)."""
    w, h = gray.size
    if max(w, h) > 1.15 * min(w, h):
        side = min(w, h)
        box = ((w - side) // 2, (h - side) // 2, (w - side) // 2 + side, (h - side) // 2 + side)
    else:
        box = (w // 10, h // 10, w - w // 10, h - h // 10)
    return phash_full(gray.crop(box))[0]


def analyse_image(path: str) -> dict:
    im, info = load_photo(path, REDUCED_SIZE)
    gray = to_gray(im)
    h, std, variants = phash_full(gray)
    a = np.asarray(gray, dtype=np.float32)
    score, lap, tiles = blur_measures(gray)
    taken_at, taken_str, camera = exif_capture(info["exif"])
    return {
        "phash": h,
        "sharpness": score,
        "exposure": {"median": float(np.median(a)),
                     "black": round(float((a <= 5).mean()), 4),
                     "white": round(float((a >= 250).mean()), 4)},
        "taken_at": taken_at,
        "camera": camera or None,
        "meta": {"w": info["w"], "h": info["h"], "format": info["format"],
                 "flat": hash_is_flat(h, std), "tiles": [round(t, 2) for t in tiles],
                 "taken": taken_str, "lap": lap, "phash_var": variants,
                 "phash_crop": centre_crop_hash(gray), "chroma": chroma_signature(im)},
    }


def pixel_fingerprint(path: str) -> str:
    """SHA-256 of width, height, mode and pixels after rotation."""
    im, _ = load_photo(path, None)
    if im.mode == "P":
        im = im.convert("RGBA")
    if im.mode in ("RGBA", "LA"):
        if im.getchannel("A").getextrema() == (255, 255):
            im = im.convert("RGB" if im.mode == "RGBA" else "L")
    elif im.mode not in ("RGB", "L") and not im.mode.startswith("I"):
        im = im.convert("RGB")
    h = hashlib.sha256(f"{im.width}x{im.height}:{im.mode}:".encode())
    h.update(im.tobytes())
    return h.hexdigest()


# --------------------------------------------------------------------------
# PDFs: text, scanned pages and OCR
# --------------------------------------------------------------------------

def find_tesseract() -> str | None:
    exe = shutil.which("tesseract")
    if exe:
        return exe
    if IS_WINDOWS:
        for base in (os.environ.get("ProgramFiles", r"C:\Program Files"),
                     os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
                     os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs")):
            cand = os.path.join(base, "Tesseract-OCR", "tesseract.exe")
            if os.path.isfile(cand):
                return cand
    return None


def ocr_image(img, tesseract: str, lang: str, timeout: float) -> str:
    fd, tmp = tempfile.mkstemp(suffix=".png", prefix="dupefinder-ocr-")
    os.close(fd)
    try:
        img.save(tmp)
        flags = subprocess.CREATE_NO_WINDOW if IS_WINDOWS else 0
        # One thread per Tesseract: DupeFinder already runs one per worker, and
        # Tesseract's own threads slow to a crawl when several copies compete.
        env = dict(os.environ, OMP_THREAD_LIMIT="1")
        out = subprocess.run([tesseract, tmp, "stdout", "-l", lang], capture_output=True,
                             timeout=timeout, creationflags=flags, env=env)
        return out.stdout.decode("utf-8", "replace")
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass


def _render_gray(page, dpi: int):
    pm = get_pymupdf()
    pix = page.get_pixmap(dpi=dpi, colorspace=pm.csGRAY, alpha=False)
    return Image.frombytes("L", (pix.width, pix.height), pix.samples)


def pdf_texts(doc, settings: dict, scanned_pages: list[int]) -> tuple[list[str], bool]:
    """Page texts used for the text checks; OCR'd text for scanned pages with --ocr."""
    texts = [doc[i].get_text("text") for i in range(doc.page_count)]
    tess = settings.get("tesseract")
    if not tess or not scanned_pages:
        return texts, False
    for i in scanned_pages:
        img = _render_gray(doc[i], 200)
        texts[i] = ocr_image(img, tess, settings.get("ocr_lang", "eng"), 50)
    return texts, True


def analyse_pdf(path: str, settings: dict) -> dict:
    pm = get_pymupdf()
    doc = pm.open(io_path(path))
    try:
        if doc.needs_pass and not doc.authenticate(""):
            return {"meta": {"encrypted": True}}
        n = doc.page_count
        raw = [doc[i].get_text("text") for i in range(n)]
        chars = [len(re.sub(r"\s+", "", t)) for t in raw]
        avg = sum(chars) / max(n, 1)
        scanned = avg < SCANNED_CHARS_PER_PAGE
        window = min(n, settings["max_pages"])
        render = list(range(window)) if scanned else [i for i in range(window)
                                                      if chars[i] < SCANNED_CHARS_PER_PAGE]
        hashes, flat_pages = [], []
        for i in render:
            h, std = phash_gray(_render_gray(doc[i], 72))
            hashes.append(h)
            if hash_is_flat(h, std):
                flat_pages.append(i)
        texts, ocr_used = raw, False
        if settings.get("tesseract") and render:
            texts, ocr_used = pdf_texts(doc, settings, render)
        out = {
            "page_hashes": hashes or None,
            "meta": {"pages": n, "chars_per_page": round(avg, 1), "scanned": scanned,
                     "mixed": bool(render) and not scanned, "hashed_pages": render,
                     "flat_pages": flat_pages, "ocr": ocr_used,
                     "max_pages": settings["max_pages"], "ocr_on": bool(settings.get("tesseract"))},
        }
        if not scanned or ocr_used:
            norm = normalize_pages(texts)
            if norm:
                out["content_sha"] = hashlib.sha256(norm.encode("utf-8")).hexdigest()
                sh = shingle_set(WORD_RE.findall(norm))
                out["meta"]["shingles"] = int(len(sh))
                if len(sh) >= MIN_SHINGLES:
                    out["minhash"] = minhash_signature(sh)
        return out
    finally:
        doc.close()


def pdf_shingles(path: str, settings: dict):
    pm = get_pymupdf()
    doc = pm.open(io_path(path))
    try:
        if doc.needs_pass and not doc.authenticate(""):
            return np.empty(0, dtype=np.uint64)
        raw = [doc[i].get_text("text") for i in range(doc.page_count)]
        texts = raw
        if settings.get("tesseract"):
            chars = [len(re.sub(r"\s+", "", t)) for t in raw]
            window = min(doc.page_count, settings["max_pages"])
            if sum(chars) / max(doc.page_count, 1) < SCANNED_CHARS_PER_PAGE:
                texts, _ = pdf_texts(doc, settings, list(range(window)))
        return shingle_set(WORD_RE.findall(normalize_pages(texts)))
    finally:
        doc.close()


# --------------------------------------------------------------------------
# Worker processes (killable, so every file gets at most --timeout seconds)
# --------------------------------------------------------------------------

def describe_error(e: BaseException) -> str:
    msg = str(e).strip().splitlines()[0] if str(e).strip() else ""
    return f"{type(e).__name__}: {msg}" if msg else type(e).__name__


def _op_analyse(arg: dict, settings: dict) -> dict:
    out: dict = {}
    path = arg["path"]
    if arg.get("sha"):
        out["file_sha"] = sha256_file(path)
    if arg.get("analyse"):
        try:
            if arg["kind"] == "image":
                out.update(analyse_image(path))
            else:
                out.update(analyse_pdf(path, settings))
        except Exception as e:
            out["error"] = describe_error(e)
    return out


def _op_pixsha(arg: dict, settings: dict) -> str:
    return pixel_fingerprint(arg["path"])


def _op_shingles(arg: dict, settings: dict) -> bytes:
    return pdf_shingles(arg["path"], settings).tobytes()


WORKER_OPS = {"analyse": _op_analyse, "pixsha": _op_pixsha, "shingles": _op_shingles}


def _worker_setup():
    install_network_guard()
    try:
        signal.signal(signal.SIGINT, signal.SIG_IGN)   # the main process handles Ctrl+C
    except (ValueError, OSError):
        pass
    warnings.simplefilter("ignore")
    if Image is not None:
        Image.MAX_IMAGE_PIXELS = 300_000_000
        enable_heif()


def _worker_main(conn, settings: dict):
    _worker_setup()
    while True:
        try:
            msg = conn.recv()
        except (EOFError, OSError):
            return
        if msg is None:
            return
        op, arg = msg
        try:
            conn.send((True, WORKER_OPS[op](arg, settings)))
        except Exception as e:
            try:
                conn.send((False, describe_error(e)))
            except Exception:
                return


class WorkerPool:
    """Process pool where a stuck task can be killed without losing the others.

    Each worker has its own pipe, so killing one can't corrupt a shared queue.
    """

    def __init__(self, size: int, settings: dict, timeout: float):
        self.ctx = multiprocessing.get_context("spawn")
        self.size = max(1, size)
        self.settings = settings
        self.timeout = timeout
        self.workers: list[dict] = []
        self.successes = 0
        self.crashes = 0

    def _crashed(self):
        self.crashes += 1
        if not self.successes and self.crashes >= 4:
            raise RuntimeError("Worker processes keep stopping before finishing any file. "
                               "Check the log, or try --workers 1.")

    def _spawn(self) -> dict:
        parent, child = self.ctx.Pipe(duplex=True)
        proc = self.ctx.Process(target=_worker_main, args=(child, self.settings),
                                daemon=True, name="dupefinder-worker")
        proc.start()
        child.close()
        return {"proc": proc, "conn": parent, "key": None, "start": 0.0}

    def _retire(self, w: dict):
        try:
            w["proc"].kill()
            w["proc"].join(5)
        except Exception:
            pass
        try:
            w["conn"].close()
        except Exception:
            pass

    def run(self, tasks):
        """Run (key, op, arg) tasks; yield (key, ok, result_or_error) as they finish."""
        tasks = iter(tasks)
        pending = next(tasks, None)
        try:
            while pending is not None or any(w["key"] is not None for w in self.workers):
                if pending is not None and len(self.workers) < self.size and \
                        all(w["key"] is not None for w in self.workers):
                    self.workers.append(self._spawn())
                for i, w in enumerate(self.workers):
                    if w["key"] is None and pending is not None:
                        key, op, arg = pending
                        pending = next(tasks, None)
                        try:
                            w["conn"].send((op, arg))
                        except (OSError, ValueError):
                            self._retire(w)
                            self.workers[i] = w = self._spawn()
                            w["conn"].send((op, arg))
                        w["key"], w["start"] = key, time.monotonic()
                waitables = []
                for w in self.workers:
                    if w["key"] is not None:
                        waitables += [w["conn"], w["proc"].sentinel]
                ready = multiprocessing.connection.wait(waitables, timeout=0.5)
                now = time.monotonic()
                for i, w in enumerate(self.workers):
                    key = w["key"]
                    if key is None:
                        continue
                    if w["conn"] in ready:
                        try:
                            ok, res = w["conn"].recv()
                        except (EOFError, OSError):
                            ok, res = False, f"{WORKER_CRASH} stopped unexpectedly"
                            self._retire(w)
                            self._crashed()
                            self.workers[i] = self._spawn()
                        else:
                            w["key"] = None
                            self.successes += 1
                        yield key, ok, res
                    elif w["proc"].sentinel in ready:
                        self._retire(w)
                        self._crashed()
                        self.workers[i] = self._spawn()
                        yield key, False, f"{WORKER_CRASH} crashed on this file"
                    elif now - w["start"] > self.timeout:
                        self._retire(w)
                        self.workers[i] = self._spawn()
                        yield key, False, f"Timed out after {self.timeout:.0f} seconds"
        except BaseException:
            self.close()
            raise

    def close(self):
        for w in self.workers:
            try:
                if w["key"] is None:
                    w["conn"].send(None)
            except Exception:
                pass
        deadline = time.monotonic() + 3
        for w in self.workers:
            w["proc"].join(max(0.0, deadline - time.monotonic()))
            if w["proc"].is_alive():
                self._retire(w)
            else:
                try:
                    w["conn"].close()
                except Exception:
                    pass
        self.workers = []


# --------------------------------------------------------------------------
# Matcher: candidate search, verification and scores
# --------------------------------------------------------------------------

LINK_PRIORITY = {"identical": 0, "same_text": 1, "same_pixels": 2, "similar_text": 3,
                 "burst": 4, "visual": 5, "visual_pdf": 6, "ai": 7, "related": 8}
PRESELECT_KINDS = {"identical", "same_text", "same_pixels", "similar_text", "visual",
                   "visual_pdf", "burst", "ai"}


def bitcount(x):
    if hasattr(np, "bitwise_count"):
        return np.bitwise_count(x)
    b = np.ascontiguousarray(x).view(np.uint8).reshape(x.shape + (8,))
    return np.unpackbits(b, axis=-1).sum(axis=-1)


MAX_MATCHES_PER_FILE = 32   # closest matches kept per file; caps memory on huge near-identical sets


def hamming_pairs(h1, thresh: int, h2=None, threads: int = 1, progress=None):
    """Index pairs within `thresh` differing bits, compared in blocks with NumPy.

    With one array: pairs i < j inside it. With two: every (i in h1, j in h2).
    Blocks run on several threads (NumPy releases the GIL). Each row keeps at
    most MAX_MATCHES_PER_FILE closest matches, so thousands of near-identical
    images can't exhaust memory; groups still join through the kept links.
    Returns three int arrays (i, j, bits).
    """
    a = np.asarray(h1, dtype=np.uint64)
    b = a if h2 is None else np.asarray(h2, dtype=np.uint64)
    empty = (np.empty(0, np.int64), np.empty(0, np.int64), np.empty(0, np.int64))
    if len(a) == 0 or len(b) == 0:
        return empty
    block = max(1, (1 << 22) // len(b))
    starts = list(range(0, len(a), block))
    cap = MAX_MATCHES_PER_FILE

    def run_block(s):
        e = min(len(a), s + block)
        cols = b[s:] if h2 is None else b
        off = s if h2 is None else 0
        d = bitcount(a[s:e, None] ^ cols[None, :])
        mask = d <= thresh
        if h2 is None:
            mask &= np.arange(off, off + d.shape[1])[None, :] > np.arange(s, e)[:, None]
        counts = mask.sum(axis=1)
        if counts.max(initial=0) > cap:
            for r in np.nonzero(counts > cap)[0]:
                row = np.where(mask[r], d[r], 255)
                keep = np.argpartition(row, cap)[:cap]
                mask[r] = False
                mask[r, keep] = True
        ii, jj = np.nonzero(mask)
        return ii + s, jj + off, d[ii, jj].astype(np.int64)

    parts, done = [], 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, threads)) as ex:
        for res in ex.map(run_block, starts):
            parts.append(res)
            done += 1
            if progress and done % 50 == 0:
                progress(done / len(starts))
    if not parts:
        return empty
    return tuple(np.concatenate([p[k] for p in parts]) for k in range(3))


def hamming(a: int, b: int) -> int:
    return popcount64(a ^ b)


def visual_score(bits: int) -> float:
    return (HASH_BITS - bits) / HASH_BITS * 100.0


def pdf_visual_score(a: FileRec, b: FileRec, max_bits: int) -> float:
    """Mean score of matched pages x matched pages / the larger page count.

    Computed from both sides and the lower value taken, so a PDF that repeats
    one page can't score as a full match.
    """
    if not a.page_hashes or not b.page_hashes:
        return 0.0
    ha = np.asarray(a.page_hashes, dtype=np.uint64)
    hb = np.asarray(b.page_hashes, dtype=np.uint64)
    d = bitcount(ha[:, None] ^ hb[None, :])
    na, nb = a.meta.get("pages") or len(ha), b.meta.get("pages") or len(hb)
    both_scanned = a.meta.get("scanned") and b.meta.get("scanned")

    def side(best):
        matched = best <= max_bits
        m = int(matched.sum())
        if not m:
            return 0.0
        mean = float(((HASH_BITS - best[matched]) / HASH_BITS).mean())
        if both_scanned:
            # Pages past --max-pages weren't hashed: extrapolate from the window.
            est = min(m / min(len(ha), len(hb)) * min(na, nb), min(na, nb))
            return mean * est / max(na, nb) * 100
        return mean * m / max(na, nb) * 100

    return min(side(d.min(axis=1)), side(d.min(axis=0)))


def pdf_has_extra_pages(r: FileRec, k: FileRec, max_bits: int) -> bool:
    """True if PDF r has more pages than k, or image-only pages k lacks."""
    if (r.meta.get("pages") or 0) > (k.meta.get("pages") or 0):
        return True
    flat = set(r.meta.get("flat_pages") or [])
    mine = [h for idx, h in zip(r.meta.get("hashed_pages") or [], r.page_hashes or [])
            if idx not in flat]
    theirs = list(k.page_hashes or [])
    return any(all(hamming(h, t) > max_bits for t in theirs) for h in mine)


def colour_ok(a: FileRec, b: FileRec) -> bool:
    """False when two photos clearly differ in colour (same layout, other colours).

    Black-and-white or nearly colourless photos always pass, so a greyscale
    conversion still matches its colour original.
    """
    ca, cb = a.meta.get("chroma"), b.meta.get("chroma")
    if not ca or not cb or min(ca[2], cb[2]) < 6:
        return True
    return abs(ca[0] - cb[0]) + abs(ca[1] - cb[1]) <= COLOUR_DIFF_CUT


VARIANT_NAMES = ("rotated", "rotated", "rotated", "mirrored")


def variant_match(r: FileRec, k: FileRec):
    """(bits, how) for the closest rotated, mirrored or cropped relation, or None."""
    best = None
    for name, h in zip(VARIANT_NAMES, k.meta.get("phash_var") or []):
        d = hamming(r.phash, h)
        if best is None or d < best[0]:
            best = (d, name)
    for x, y in ((r.phash, k.meta.get("phash_crop")), (k.phash, r.meta.get("phash_crop"))):
        if x is not None and y is not None:
            d = hamming(x, y)
            if best is None or d < best[0]:
                best = (d, "cropped")
    return best


@dataclass(slots=True)
class Link:
    a: int
    b: int
    kind: str
    score: float | None
    extra: dict = field(default_factory=dict)


class Matcher:
    def __init__(self, recs: list[FileRec], opts, compute, progress=None):
        """`compute(op, fids)` fills pixel fingerprints or returns shingle sets."""
        self.recs = recs
        self.opts = opts
        self.compute = compute
        self.progress = progress
        self.links: dict[tuple, Link] = {}
        self.shingles: dict[int, object] = {}
        self.burst_chain: dict[int, int] = {}
        self.burst_chains: list[list[int]] = []
        self.ai_sim = None

    # -- helpers -------------------------------------------------------
    def add(self, a: int, b: int, kind: str, score: float | None, **extra):
        if a == b:
            return
        key = (min(a, b), max(a, b))
        old = self.links.get(key)
        if old is None or LINK_PRIORITY[kind] < LINK_PRIORITY[old.kind] or (
                kind == old.kind and (score or 0) > (old.score or 0)):
            self.links[key] = Link(key[0], key[1], kind, score, extra)

    def hp(self, what: str) -> dict:
        """Thread count and progress reporting for hamming_pairs()."""
        report = None
        if self.progress:
            report = lambda frac: self.progress(f"Comparing {what}: {frac:.0%}")
        return {"threads": self.opts.workers, "progress": report}

    def photos(self):
        return [r for r in self.recs if r.kind == "image" and r.phash is not None and not r.error]

    def pdfs(self):
        return [r for r in self.recs if r.kind == "pdf" and not r.error]

    # -- checks --------------------------------------------------------
    def match_all(self):
        o = self.opts
        self.match_fingerprints()
        self.match_photos()
        self.match_bursts()
        if "pdf" in o.type_set:
            self.match_text()
            self.match_scanned_pdfs()
            if "image" in o.type_set:
                self.match_cross_type()
        if o.ai:
            self.match_ai()
        return list(self.links.values())

    def match_fingerprints(self):
        by_sha = collections.defaultdict(list)
        by_text = collections.defaultdict(list)
        for r in self.recs:
            if r.file_sha:
                by_sha[r.file_sha].append(r.fid)
            if r.kind == "pdf" and r.content_sha:
                by_text[r.content_sha].append(r.fid)
        for fids in by_sha.values():
            for f in fids[1:]:
                self.add(fids[0], f, "identical", 100.0)
        for fids in by_text.values():
            for f in fids[1:]:
                self.add(fids[0], f, "same_text", 100.0)

    def match_photos(self):
        o = self.opts
        photos = [r for r in self.photos() if not r.flat]
        hashes = [r.phash for r in photos]
        ii, jj, dd = hamming_pairs(hashes, max(o.max_bits, PIXEL_CANDIDATE_BITS), **self.hp("photos"))
        need, pixel_pairs = set(), []
        for i, j, d in zip(ii.tolist(), jj.tolist(), dd.tolist()):
            a, b = photos[i], photos[j]
            if (d <= PIXEL_CANDIDATE_BITS and a.meta.get("w") == b.meta.get("w")
                    and a.meta.get("h") == b.meta.get("h")
                    and not (a.file_sha and a.file_sha == b.file_sha)):
                pixel_pairs.append((a, b))
                need.update(r.fid for r in (a, b) if not r.content_sha)
            if d <= o.max_bits and colour_ok(a, b):
                self.add(a.fid, b.fid, "visual", visual_score(d), bits=d)
        if not o.quick:
            self.match_photo_variants(photos)
        if need:
            self.compute("pixsha", sorted(need))
        for a, b in pixel_pairs:
            if a.content_sha and a.content_sha == b.content_sha:
                self.add(a.fid, b.fid, "same_pixels", 100.0)

    def match_photo_variants(self, photos):
        """Copies turned or mirrored in their pixels (no rotation flag) and crops."""
        o = self.opts
        hashes = [r.phash for r in photos]
        owners, names, extra = [], [], []
        for i, r in enumerate(photos):
            for name, h in zip(VARIANT_NAMES, r.meta.get("phash_var") or []):
                owners.append(i)
                names.append(name)
                extra.append(h)
            if r.meta.get("phash_crop") is not None:
                owners.append(i)
                names.append("cropped")
                extra.append(r.meta["phash_crop"])
        ii, jj, dd = hamming_pairs(hashes, o.max_bits, extra, **self.hp("turned, mirrored and cropped photos"))
        for i, j, d in zip(ii.tolist(), jj.tolist(), dd.tolist()):
            a, b = photos[i], photos[owners[j]]
            if a is not b and colour_ok(a, b):
                self.add(a.fid, b.fid, "visual", visual_score(d), bits=d, how=names[j])

    def match_bursts(self):
        """Chain shots from one camera taken within --burst-gap seconds that look alike."""
        o = self.opts
        by_cam = collections.defaultdict(list)
        for r in self.photos():
            if r.taken_at is not None:
                by_cam[r.camera or ""].append(r)
        for shots in by_cam.values():
            shots.sort(key=lambda r: (r.taken_at, r.path))
            chain = [shots[0]]
            for prev, cur in zip(shots, shots[1:]):
                gap = cur.taken_at - prev.taken_at
                bits = hamming(prev.phash, cur.phash)
                alike = not prev.flat and not cur.flat and bits <= BURST_BITS
                if not alike and o.ai:
                    alike = self.ai_similarity(prev, cur) >= o.ai_burst_score / 100
                if gap <= o.burst_gap and alike:
                    if not (gap == 0 and bits <= o.max_bits):
                        # Same capture time and visually the same = a copy, not a frame.
                        self.add(prev.fid, cur.fid, "burst", None)
                    chain.append(cur)
                else:
                    self._close_chain(chain)
                    chain = [cur]
            self._close_chain(chain)

    def _close_chain(self, chain):
        if len({r.taken_at for r in chain}) < 2:
            return
        idx = len(self.burst_chains)
        self.burst_chains.append([r.fid for r in chain])
        for r in chain:
            self.burst_chain[r.fid] = idx

    def match_text(self):
        """MinHash LSH for candidates, then the exact 5-word-chunk overlap."""
        o = self.opts
        reps, seen = [], set()
        for r in self.pdfs():
            if r.minhash is None:
                continue
            if r.content_sha in seen:
                continue            # same text as a file already indexed
            seen.add(r.content_sha)
            reps.append(r)
        if len(reps) < 2:
            return
        target = max(0.05, o.min_score / 100 - 0.02)
        bands, rows = lsh_params(target)
        buckets = collections.defaultdict(list)
        for r in reps:
            sig = np.asarray(r.minhash, dtype=np.uint64)
            for b in range(bands):
                buckets[(b, sig[b * rows:(b + 1) * rows].tobytes())].append(r.fid)
        pairs = set()
        for fids in buckets.values():
            if len(fids) < 2:
                continue
            if len(fids) <= 100:
                pairs.update((fids[i], fids[j]) for i in range(len(fids))
                             for j in range(i + 1, len(fids)))
            else:
                # Very full buckets: compare neighbours only (links still chain).
                pairs.update((fids[i], fids[j]) for i in range(len(fids))
                             for j in range(i + 1, min(len(fids), i + 6)))
        pairs = {p for p in pairs if (min(p), max(p)) not in self.links}
        self.verify_text_pairs(pairs)

    def ensure_shingles(self, fids):
        need = [f for f in fids if f not in self.shingles]
        if need:
            for fid, arr in self.compute("shingles", need).items():
                self.shingles[fid] = arr

    def text_jaccard(self, a: int, b: int) -> float | None:
        if a not in self.shingles or b not in self.shingles:
            return None
        return exact_jaccard(self.shingles[a], self.shingles[b]) * 100

    def verify_text_pairs(self, pairs):
        if not pairs:
            return
        self.ensure_shingles(sorted({f for p in pairs for f in p}))
        for a, b in pairs:
            j = self.text_jaccard(a, b)
            if j is not None and j >= self.opts.min_score:
                self.add(a, b, "similar_text", j)

    def match_scanned_pdfs(self):
        o = self.opts
        pdfs = [r for r in self.pdfs() if r.page_hashes]
        owners, hashes = [], []
        for r in pdfs:
            flat = set(r.meta.get("flat_pages") or [])
            for idx, h in zip(r.meta.get("hashed_pages") or [], r.page_hashes):
                if idx not in flat:
                    owners.append(r)
                    hashes.append(h)
        ii, jj, _ = hamming_pairs(hashes, o.max_bits, **self.hp("scanned PDF pages"))
        cands = {(owners[i].fid, owners[j].fid) for i, j in zip(ii.tolist(), jj.tolist())
                 if owners[i].fid != owners[j].fid}
        for a, b in {(min(p), max(p)) for p in cands}:
            s = pdf_visual_score(self.recs[a], self.recs[b], o.max_bits)
            if s >= o.min_score:
                self.add(a, b, "visual_pdf", s)

    def match_cross_type(self):
        o = self.opts
        photos = [r for r in self.photos() if not r.flat]
        owners, pages, hashes = [], [], []
        for r in self.pdfs():
            if not r.page_hashes:
                continue
            flat = set(r.meta.get("flat_pages") or [])
            for idx, h in zip(r.meta.get("hashed_pages") or [], r.page_hashes):
                if idx not in flat:
                    owners.append(r)
                    pages.append(idx)
                    hashes.append(h)
        ii, jj, dd = hamming_pairs([p.phash for p in photos], o.max_bits, hashes,
                                   **self.hp("photos against PDF pages"))
        for i, j, d in zip(ii.tolist(), jj.tolist(), dd.tolist()):
            self.add(photos[i].fid, owners[j].fid, "related", visual_score(d), page=pages[j] + 1)

    # -- optional AI similarity -----------------------------------------
    def ai_similarity(self, a: FileRec, b: FileRec) -> float:
        if a.embedding is None or b.embedding is None:
            return 0.0
        return float(np.dot(a.embedding, b.embedding))

    def match_ai(self):
        photos = [r for r in self.photos() if r.embedding is not None]
        if len(photos) < 2:
            return
        emb = np.stack([np.asarray(r.embedding, dtype=np.float32) for r in photos])
        cut = self.opts.ai_min_score / 100
        block = max(1, (1 << 22) // len(photos))
        for s in range(0, len(photos), block):
            sim = emb[s:s + block] @ emb.T
            ii, jj = np.nonzero(sim >= cut)
            for i, j in zip(ii.tolist(), jj.tolist()):
                if j > i + s:
                    self.add(photos[i + s].fid, photos[j].fid, "ai", float(sim[i, j]) * 100)


# --------------------------------------------------------------------------
# Grouping: union-find, keep rules and scores against the kept copy
# --------------------------------------------------------------------------

class UnionFind:
    def __init__(self):
        self.parent: dict[int, int] = {}

    def find(self, x: int) -> int:
        self.parent.setdefault(x, x)
        root = x
        while self.parent[root] != root:
            root = self.parent[root]
        while self.parent[x] != root:
            self.parent[x], x = root, self.parent[x]
        return root

    def union(self, a: int, b: int):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[max(ra, rb)] = min(ra, rb)


@dataclass(slots=True)
class Member:
    fid: int
    keeper: bool = False
    suggest: str = "keep"
    label_kind: str = "keeper"
    score: float | None = None
    label: str = "Kept copy"
    detail: str = ""
    rel_sharpness: float | None = None
    flags: list = field(default_factory=list)
    locked: bool = False
    lock_reason: str = ""


@dataclass
class Group:
    gid: int
    type: str
    kind: str
    title: str
    members: list
    keeper: int | None
    burst_shots: int | None = None
    total: int | None = None          # poor shots flagged in all (the list is capped)

    def space(self, recs) -> int:
        return sum(recs[m.fid].size for m in self.members if m.suggest == "trash")

    def min_score(self):
        vals = [m.score for m in self.members if not m.keeper and m.score is not None]
        return min(vals) if vals else None


def link_label(kind: str, score: float | None, extra: dict | None = None, shots: int = 0) -> str:
    extra = extra or {}
    if kind == "identical":
        return "100% identical file"
    if kind == "same_text":
        return "100% same text"
    if kind == "same_pixels":
        return "100% same pixels"
    if kind == "similar_text":
        return f"{math.floor(score)}% similar text"
    if kind == "visual":
        how = f" ({extra['how']})" if extra.get("how") else ""
        return f"{score:.1f}".rstrip("0").rstrip(".") + f"% visual match{how}"
    if kind == "visual_pdf":
        return f"{math.floor(score)}% visual match"
    if kind == "burst":
        return f"Burst · {shots} shots" if shots else "Burst shot"
    if kind == "ai":
        return f"{math.floor(score)}% similar content"
    if kind == "related":
        page = extra.get("page")
        return f"Related · matches page {page}" if page else "Related"
    return kind


def detail_sharpness(r: FileRec) -> float:
    return float(r.meta.get("lap") or 0.0)


def quality_score(r: FileRec, opts) -> float:
    s = detail_sharpness(r)
    return s * 0.5 if exposure_flags(r, opts) else s


def exposure_flags(r: FileRec, opts) -> list[str]:
    e = r.exposure
    if not e:
        return []
    flags = []
    if e["median"] < opts.dark_cutoff:
        flags.append("dark")
    if (e["black"] + e["white"]) * 100 > opts.clip_cutoff:
        flags.append("clipped")
    return flags


def exposure_text(r: FileRec, opts) -> str | None:
    e = r.exposure
    if not e:
        return None
    parts = []
    if e["median"] < opts.dark_cutoff:
        parts.append(f"Dark (median {e['median']:.0f})")
    clip = (e["black"] + e["white"]) * 100
    if clip > opts.clip_cutoff:
        side = "highlights" if e["white"] >= e["black"] else "shadows"
        parts.append(f"Clipped {clip:.0f}% (mostly {side})")
    return ", ".join(parts) if parts else "OK"


class Grouper:
    def __init__(self, recs: list[FileRec], matcher: Matcher, opts):
        self.recs = recs
        self.m = matcher
        self.opts = opts
        self.prefer_keys = [path_key(p) for p in opts.prefer]

    def preferred(self, r: FileRec) -> bool:
        return any(is_under(r.path, k) for k in self.prefer_keys)

    def build(self, links: list[Link]):
        uf = UnionFind()
        adj = collections.defaultdict(list)
        for ln in links:
            uf.union(ln.a, ln.b)
            adj[ln.a].append((ln.b, ln))
            adj[ln.b].append((ln.a, ln))
        comps = collections.defaultdict(list)
        for f in list(uf.parent):
            comps[uf.find(f)].append(f)
        comps = [sorted(c) for c in comps.values() if len(c) > 1]

        keepers = {tuple(c): self.choose_keeper(c) for c in comps}
        self.prefetch(comps, keepers)
        groups = [self.make_group(c, keepers[tuple(c)], adj) for c in comps]
        groups.sort(key=lambda g: (-g.space(self.recs), -len(g.members), g.members[0].fid))
        for i, g in enumerate(groups, 1):
            g.gid = i
        grouped = {m.fid for g in groups for m in g.members}
        poor = self.poor_group(grouped, len(groups) + 1)
        if poor:
            groups.append(poor)
        return groups

    # -- keeper -------------------------------------------------------
    def choose_keeper(self, fids: list[int]) -> int:
        o = self.opts
        pool = [self.recs[f] for f in fids]
        pref = [r for r in pool if self.preferred(r)]
        if pref:
            pool = pref
        # Keep the user's own copy rather than one inside a system folder.
        ok = [r for r in pool if not r.system]
        pool = ok or pool
        kinds = collections.Counter(r.kind for r in pool)
        if len(kinds) > 1:
            major = "image" if kinds["image"] > kinds["pdf"] else "pdf"
            pool = [r for r in pool if r.kind == major]
        rule = o.keep
        inf = float("inf")
        if rule == "newest":
            key = lambda r: (-r.mtime_ns, r.path)
        elif rule == "oldest":
            key = lambda r: (r.mtime_ns, r.path)
        elif rule == "largest":
            key = lambda r: (-r.size, r.path)
        elif rule == "highest-res":
            key = lambda r: (-(r.pixels or r.meta.get("pages") or 0), -r.size, r.path)
        elif rule == "shortest-path":
            key = lambda r: (len(r.path), r.path)
        elif pool[0].kind == "image":
            frames = [r for r in pool if r.fid in self.m.burst_chain]
            if frames:
                # Only burst frames compete on sharpness; a smaller export or
                # copy of a frame must never outrank the full-size originals.
                pool = frames
                key = lambda r: (-quality_score(r, o), -r.pixels, r.path)
            else:
                key = lambda r: (-r.pixels, -r.size, has_copy_marker(r.path),
                                 r.taken_at if r.taken_at is not None else inf, r.path)
        elif all(r.meta.get("scanned") for r in pool):
            # Scans with no text: the bigger file is the better-quality scan.
            key = lambda r: (has_copy_marker(r.path), -r.size, -r.mtime_ns, len(r.path), r.path)
        else:
            key = lambda r: (has_copy_marker(r.path), -r.mtime_ns,
                             -(r.meta.get("pages") or 0), len(r.path), r.path)
        return min(pool, key=key).fid

    # -- direct relation to the kept copy --------------------------------
    def prefetch(self, comps, keepers):
        """Fetch shingle sets and pixel fingerprints needed for direct scores."""
        o = self.opts
        sh_need, px_need = set(), set()
        for c in comps:
            k = self.recs[keepers[tuple(c)]]
            for f in c:
                r = self.recs[f]
                if f == k.fid or r.kind != k.kind:
                    continue
                if r.kind == "pdf" and r.minhash is not None and k.minhash is not None \
                        and not (r.content_sha and r.content_sha == k.content_sha):
                    est = float(np.mean(np.asarray(r.minhash) == np.asarray(k.minhash))) * 100
                    if est >= o.min_score - 15:
                        sh_need.update((f, k.fid))
                if (r.kind == "image" and r.phash is not None and k.phash is not None
                        and hamming(r.phash, k.phash) <= PIXEL_CANDIDATE_BITS
                        and r.meta.get("w") == k.meta.get("w") and r.meta.get("h") == k.meta.get("h")):
                    px_need.update(x.fid for x in (r, k) if not x.content_sha)
        if sh_need:
            self.m.ensure_shingles(sorted(sh_need))
        if px_need:
            self.m.compute("pixsha", sorted(px_need))

    def relation(self, r: FileRec, k: FileRec):
        """Strongest check that matches r directly against the kept copy k."""
        o = self.opts
        if r.file_sha and r.file_sha == k.file_sha:
            return "identical", 100.0, {}
        if r.kind == "pdf" and k.kind == "pdf":
            if r.content_sha and r.content_sha == k.content_sha:
                return "same_text", 100.0, {}
            j = self.m.text_jaccard(r.fid, k.fid)
            if j is not None and j >= o.min_score:
                return "similar_text", j, {}
            if r.page_hashes and k.page_hashes:
                s = pdf_visual_score(r, k, o.max_bits)
                if s >= o.min_score:
                    return "visual_pdf", s, {}
            return None
        if r.kind == "image" and k.kind == "image":
            if r.content_sha and r.content_sha == k.content_sha:
                return "same_pixels", 100.0, {}
            chain = self.m.burst_chain.get(r.fid)
            if chain is not None and chain == self.m.burst_chain.get(k.fid) \
                    and r.taken_at != k.taken_at:
                return "burst", None, {}
            if r.phash is not None and k.phash is not None and not r.flat and not k.flat \
                    and colour_ok(r, k):
                d = hamming(r.phash, k.phash)
                if d <= o.max_bits:
                    return "visual", visual_score(d), {}
                v = None if o.quick else variant_match(r, k)
                if v and v[0] <= o.max_bits:
                    return "visual", visual_score(v[0]), {"how": v[1]}
            if o.ai:
                s = self.m.ai_similarity(r, k) * 100
                if s >= o.ai_min_score:
                    return "ai", s, {}
            return None
        photo, pdf = (r, k) if r.kind == "image" else (k, r)
        if photo.phash is None or not pdf.page_hashes or photo.flat:
            return None
        best, page = HASH_BITS + 1, None
        flat = set(pdf.meta.get("flat_pages") or [])
        for idx, h in zip(pdf.meta.get("hashed_pages") or [], pdf.page_hashes):
            if idx in flat:
                continue
            d = hamming(photo.phash, h)
            if d < best:
                best, page = d, idx + 1
        if best <= o.max_bits:
            return "related", visual_score(best), {"page": page}
        return None

    def make_group(self, fids: list[int], keeper: int, adj) -> Group:
        o = self.opts
        k = self.recs[keeper]
        # BFS tree from the keeper, to name the member a file was linked through.
        parent = {keeper: None}
        order = [keeper]
        for f in order:
            for nb, ln in sorted(adj[f], key=lambda t: (LINK_PRIORITY[t[1].kind], t[0])):
                if nb not in parent:
                    parent[nb] = (f, ln)
                    order.append(nb)
        burst_fids = [f for f in fids if f in self.m.burst_chain]
        is_burst = any(ln.kind == "burst" for f in fids for _, ln in adj[f])
        shots = len({(self.recs[f].camera, self.recs[f].taken_at) for f in burst_fids}) if is_burst else 0
        top_sharp = max(detail_sharpness(self.recs[f]) for f in burst_fids) if burst_fids else 0

        members = []
        for f in fids:
            r = self.recs[f]
            m = Member(fid=f)
            if f == keeper:
                m.keeper = True
            else:
                rel = self.relation(r, k)
                if rel:
                    kind, score, extra = rel
                    m.label_kind, m.score = kind, (round(score, 2) if score is not None else None)
                    m.label = link_label(kind, score, extra, shots)
                    if kind in PRESELECT_KINDS:
                        m.suggest = "trash"
                    if extra.get("how") in ("mirrored", "cropped"):
                        m.suggest = "keep"
                        m.detail = f"A {extra['how']} version may be a deliberate edit, so it isn't pre-selected"
                    if r.kind == "image" and is_raw(r.path) != is_raw(k.path):
                        m.suggest = "keep"
                        m.detail = "RAW and non-RAW versions of the same photo, so it isn't pre-selected"
                    if r.kind == "pdf" and kind != "identical" and pdf_has_extra_pages(r, k, o.max_bits):
                        m.suggest = "keep"
                        m.detail = "Has pages the kept copy doesn't, so it isn't pre-selected"
                else:
                    via, ln = parent.get(f) or (None, None)
                    m.label_kind, m.score = "via", None
                    m.label = "Linked through another file"
                    if via is not None:
                        m.detail = (f"linked through {self.recs[via].name} "
                                    f"({link_label(ln.kind, ln.score, ln.extra, shots)})")
            if r.kind == "image":
                in_burst = is_burst and f in self.m.burst_chain
                if in_burst and top_sharp:
                    m.rel_sharpness = round(detail_sharpness(r) / top_sharp * 100, 1)
                elif r.sharpness is not None and r.sharpness < o.blur_cutoff and not r.flat:
                    m.flags.append("blurry")
                m.flags += exposure_flags(r, o)
            if r.system:
                m.flags.append("system")
                m.suggest = "keep"
            if r.nlink > 1:
                m.flags.append("hardlinked")
                m.suggest = "keep"
                m.locked = True
                m.lock_reason = "Hard-linked file: another path shares its data, so it isn't moved"
            if r.meta.get("encrypted"):
                m.flags.append("protected")
            if r.error:
                m.flags.append("error")
            if self.preferred(r):
                m.flags.append("prefer")
            members.append(m)

        kinds = {self.recs[f].kind for f in fids}
        labels = [m.label_kind for m in members if not m.keeper]
        if is_burst:
            gtype, title = "burst", f"Burst · {shots} shots"
            order_key = lambda m: (self.recs[m.fid].taken_at or 0, self.recs[m.fid].path)
        elif "related" in labels or len(kinds) > 1:
            gtype, title = "related", "Photo and PDF scan" if len(kinds) > 1 else f"{len(fids)} related files"
            order_key = None
        elif all(lk == "identical" for lk in labels):
            gtype, title = "identical", f"{len(fids)} identical files"
            order_key = None
        elif all(lk in ("identical", "same_text", "same_pixels") for lk in labels):
            gtype, title = "same", f"{len(fids)} files with the same content"
            order_key = None
        else:
            gtype, title = "similar", f"{len(fids)} similar files"
            order_key = None
        if order_key is None:
            order_key = lambda m: (not m.keeper, -(m.score if m.score is not None else -1),
                                   self.recs[m.fid].path)
        members.sort(key=order_key)
        return Group(gid=0, type=gtype, kind=kinds.pop() if len(kinds) == 1 else "mixed",
                     title=title, members=members, keeper=keeper,
                     burst_shots=shots if is_burst else None)

    def poor_group(self, grouped: set, gid: int) -> Group | None:
        o = self.opts
        members = []
        for r in self.recs:
            if r.kind != "image" or r.fid in grouped or r.error or r.sharpness is None:
                continue
            flags = []
            if r.sharpness < o.blur_cutoff and not r.flat:
                flags.append("blurry")
            flags += exposure_flags(r, o)
            if not flags:
                continue
            parts = []
            if "blurry" in flags:
                parts.append(f"Sharpness {r.sharpness:.0f} (cutoff {o.blur_cutoff:g})")
            if "dark" in flags or "clipped" in flags:
                parts.append(exposure_text(r, o))
            m = Member(fid=r.fid, label_kind="poor", label="Possibly poor shot",
                       detail=", ".join(parts), flags=flags)
            if r.system:
                m.flags.append("system")
            if r.nlink > 1:
                m.flags.append("hardlinked")
                m.locked = True
                m.lock_reason = "Hard-linked file: another path shares its data, so it isn't moved"
            members.append(m)
        if not members:
            return None
        members.sort(key=lambda m: (self.recs[m.fid].sharpness or 0, self.recs[m.fid].path))
        total = len(members)
        title = f"{total:,} possibly poor shots"
        if total > MAX_POOR_LISTED:      # a list this long helps nobody: show the worst
            members = members[:MAX_POOR_LISTED]
            title = f"The {MAX_POOR_LISTED:,} worst of {total:,} possibly poor shots"
        return Group(gid=gid, type="poor", kind="image", title=title, members=members,
                     keeper=None, total=total)


# --------------------------------------------------------------------------
# Safety: drive types, Recycle Bin checks and pre-move validation
# --------------------------------------------------------------------------

WINDOWS_DRIVE_TYPES = {0: "unknown", 1: "unknown", 2: "removable", 3: "fixed",
                       4: "remote", 5: "cdrom", 6: "ramdisk"}


def volume_root(path: str) -> str:
    path = os.path.abspath(path)
    if IS_WINDOWS:
        import ctypes
        buf = ctypes.create_unicode_buffer(32768)
        if ctypes.windll.kernel32.GetVolumePathNameW(ctypes.c_wchar_p(to_extended_path(path)),
                                                      buf, 32768):
            root = buf.value
            if root.startswith("\\\\?\\UNC\\"):
                root = "\\\\" + root[8:]
            elif root.startswith("\\\\?\\"):
                root = root[4:]
            return root if root.endswith("\\") else root + "\\"
        drive, _ = os.path.splitdrive(path)
        return drive.rstrip("\\") + "\\"
    p = path
    while not os.path.ismount(p):
        parent = os.path.dirname(p)
        if parent == p:
            break
        p = parent
    return p


def drive_kind(path: str) -> str:
    """fixed, removable, remote, cdrom, ramdisk or unknown. Tests replace this."""
    if IS_WINDOWS:
        import ctypes
        t = ctypes.windll.kernel32.GetDriveTypeW(ctypes.c_wchar_p(volume_root(path)))
        return WINDOWS_DRIVE_TYPES.get(t, "unknown")
    root = volume_root(path)
    if root != "/" and any(root.startswith(p) for p in ("/media/", "/run/media/", "/mnt/", "/Volumes/")):
        return "removable"
    return "fixed"


def _reg_value(hive, key: str, name: str):
    import winreg
    try:
        with winreg.OpenKey(hive, key) as k:
            return winreg.QueryValueEx(k, name)[0]
    except OSError:
        return None


def recycle_bin_problem(path: str, size: int) -> str | None:
    """Why a file on a fixed Windows drive can't safely go to the Recycle Bin.

    send2trash deletes permanently, without asking, when the bin is turned off
    for the drive or the file is bigger than the bin, so those files go to the
    review folder instead.
    """
    if not IS_WINDOWS:
        return None
    try:
        import ctypes
        import winreg
        pol = r"Software\Microsoft\Windows\CurrentVersion\Policies\Explorer"
        for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
            if _reg_value(hive, pol, "NoRecycleFiles") == 1:
                return "the Recycle Bin is turned off by policy"
        buf = ctypes.create_unicode_buffer(300)
        if ctypes.windll.kernel32.GetVolumeNameForVolumeMountPointW(
                ctypes.c_wchar_p(volume_root(path)), buf, 300):
            m = re.search(r"\{[0-9A-Fa-f-]+\}", buf.value)
            if m:
                key = r"Software\Microsoft\Windows\CurrentVersion\Explorer\BitBucket\Volume" + "\\" + m.group(0)
                if _reg_value(winreg.HKEY_CURRENT_USER, key, "NukeOnDelete") == 1:
                    return "the Recycle Bin is turned off for this drive"
                cap = _reg_value(winreg.HKEY_CURRENT_USER, key, "MaxCapacity")
                if cap and size > int(cap) * 1024 * 1024:
                    return "the file is bigger than this drive's Recycle Bin"
    except Exception as e:  # unknown setups keep the default (bin enabled)
        log.info("Recycle Bin check failed for %s: %s", path, e)
    return None


def trash_file(path: str):
    """Move one file to the Recycle Bin / Trash. Tests may replace this."""
    from send2trash import send2trash
    send2trash(path)


def review_root(path: str) -> str:
    """Where a drive's _duplicates_review folder lives. Tests replace this."""
    return volume_root(path)


def review_destination(path: str) -> str:
    root = review_root(path)
    rel = os.path.relpath(os.path.abspath(path), root)
    dest = os.path.join(root, REVIEW_DIR, rel)
    base, ext = os.path.splitext(dest)
    n = 1
    while os.path.lexists(io_path(dest)):
        dest = f"{base} ({n}){ext}"
        n += 1
    return dest


def move_to_review(path: str, dest: str | None = None) -> str:
    dest = dest or review_destination(path)
    os.makedirs(io_path(os.path.dirname(dest)), exist_ok=True)
    os.rename(io_path(path), io_path(dest))   # same drive, so this is a rename
    return dest


class Mover:
    """Moves files chosen in the report, after every safety check."""

    def __init__(self, result, cache: Cache):
        self.r = result
        self.cache = cache
        self.lock = threading.Lock()
        self.moved: dict[int, str] = {}

    def move(self, fids: list) -> list[dict]:
        with self.lock:
            return self._move(fids)

    def _move(self, fids):
        results, todo = [], []
        recs = self.r.recs
        valid = set()
        for f in fids:
            if not isinstance(f, int) or isinstance(f, bool) or not 0 <= f < len(recs) \
                    or f not in self.r.member_group:
                results.append({"fid": f, "ok": False, "reason": "Not part of this scan"})
            elif f in self.moved:
                results.append({"fid": f, "ok": False, "reason": "Already moved"})
            elif f not in valid:
                valid.add(f)
                todo.append(f)
        # No request may leave a duplicate group without a kept file.
        blocked = set()
        by_group = collections.defaultdict(set)
        for f in todo:
            by_group[self.r.member_group[f]].add(f)
        for gid, chosen in by_group.items():
            g = self.r.group_by_id[gid]
            if g.type == "poor":
                continue
            left = [m.fid for m in g.members if m.fid not in self.moved
                    and m.fid not in chosen and self._unchanged(recs[m.fid])]
            if not left:
                blocked |= chosen
        for f in todo:
            rec = recs[f]
            g = self.r.group_by_id[self.r.member_group[f]]
            if f in blocked:
                results.append({"fid": f, "ok": False,
                                "reason": f"Refused: this would leave no copy in group {g.gid}"})
                continue
            res = self._move_one(rec, g)
            res["fid"] = f
            results.append(res)
        try:
            self.cache.export_actions()
        except OSError as e:
            log.warning("Couldn't write actions.csv: %s", e)
        return results

    @staticmethod
    def _unchanged(rec: FileRec) -> bool:
        try:
            st = os.stat(io_path(rec.path))
        except OSError:
            return False
        return st.st_size == rec.size and st.st_mtime_ns == rec.mtime_ns

    def _move_one(self, rec: FileRec, g: Group) -> dict:
        try:
            st = os.stat(io_path(rec.path))
        except FileNotFoundError:
            return {"ok": False, "reason": "The file no longer exists"}
        except OSError as e:
            return {"ok": False, "reason": f"Couldn't check the file: {e.strerror or e}"}
        if st.st_size != rec.size or st.st_mtime_ns != rec.mtime_ns:
            return {"ok": False, "reason": "The file changed after the scan; rescan first"}
        if st.st_nlink > 1:
            return {"ok": False, "reason": "Hard-linked file; not moved"}
        kind = drive_kind(rec.path)
        if kind == "cdrom":
            return {"ok": False, "reason": "The file is on a read-only disc"}
        method, note = "recycle", ""
        if kind != "fixed":
            method, note = "review", f"{kind} drive has no Recycle Bin"
        else:
            problem = recycle_bin_problem(rec.path, rec.size)
            if problem:
                method, note = "review", problem
        keeper = self.r.recs[g.keeper].path if g.keeper is not None else ""
        dest = "Recycle Bin" if method == "recycle" else review_destination(rec.path)
        # Log first, so even a crash or power cut mid-move leaves a record
        # that --undo-review and actions.csv can use.
        try:
            action_id = self.cache.log_action(self.r.scan_id, rec.path, method, dest, g.gid,
                                              keeper, rec.file_sha, rec.size, note)
        except Exception as e:
            return {"ok": False, "reason": f"Couldn't write the action log, so nothing was moved: {e}"}
        try:
            if method == "recycle":
                trash_file(rec.path)
                if os.path.lexists(io_path(rec.path)):
                    self.cache.mark_failed(action_id, "the Recycle Bin didn't take the file")
                    return {"ok": False, "reason": "The Recycle Bin didn't take the file"}
            else:
                move_to_review(rec.path, dest)
        except Exception as e:
            log.warning("Move failed for %s: %s", rec.path, e)
            self.cache.mark_failed(action_id, describe_error(e))
            return {"ok": False, "reason": f"Couldn't move the file: {describe_error(e)}"}
        self.moved[rec.fid] = dest
        log.info("Moved %s -> %s", rec.path, dest)
        return {"ok": True, "action": method, "dest": dest}


def undo_review(cache: Cache) -> int:
    """Move files from _duplicates_review folders back, using the action log."""
    acts = cache.actions()
    restored = {int(a["note"][5:]) for a in acts
                if a["action"] == "restore" and (a["note"] or "").startswith("undo:")}
    done = problems = 0
    for a in acts:
        if a["action"] != "review" or not a["destination"]:
            continue
        src, orig = a["destination"], a["path"]
        if a["id"] in restored:
            continue
        if not os.path.lexists(io_path(src)):
            print(f"  Not found (already moved back?): {src}")
            problems += 1
            continue
        if os.path.lexists(io_path(orig)):
            print(f"  Skipped, a file already exists at: {orig}")
            problems += 1
            continue
        try:
            os.makedirs(io_path(os.path.dirname(orig)), exist_ok=True)
            os.rename(io_path(src), io_path(orig))
        except OSError as e:
            print(f"  Couldn't move back {src}: {e}")
            problems += 1
            continue
        cache.log_action(a["scan_id"], orig, "restore", src, a["group_id"], a["kept_path"],
                         a["file_sha256"], a["size"], f"undo:{a['id']}")
        _prune_empty_dirs(os.path.dirname(src))
        done += 1
    cache.export_actions()
    print(f"Moved {done} file(s) back to their original folders"
          + (f"; {problems} couldn't be moved (listed above)." if problems else "."))
    return 0 if not problems else 1


def _prune_empty_dirs(folder: str):
    while os.path.basename(folder).casefold() != REVIEW_DIR and REVIEW_DIR in folder.casefold():
        try:
            os.rmdir(io_path(folder))
        except OSError:
            return
        folder = os.path.dirname(folder)
    try:
        if os.path.basename(folder).casefold() == REVIEW_DIR:
            os.rmdir(io_path(folder))
    except OSError:
        pass


def open_in_system(path: str, reveal: bool):
    if IS_WINDOWS:
        if reveal:
            subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])
        else:
            os.startfile(path)  # opens with the default app
    elif IS_MAC:
        subprocess.Popen(["open", "-R", path] if reveal else ["open", path])
    else:
        subprocess.Popen(["xdg-open", os.path.dirname(path) if reveal else path],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


# --------------------------------------------------------------------------
# Thumbnails, full views and 100% crops for the report
# --------------------------------------------------------------------------

def _rgb_for_jpeg(im):
    if im.mode in ("RGBA", "LA", "P"):
        im = im.convert("RGBA")
        bg = Image.new("RGB", im.size, (255, 255, 255))
        bg.paste(im, mask=im.getchannel("A"))
        return bg
    if im.mode != "RGB":
        return to_gray(im).convert("RGB") if im.mode.startswith("I") else im.convert("RGB")
    return im


def make_thumbnail(rec: FileRec, thumbs_dir: str, size: int = 256) -> str | None:
    key = hashlib.sha1(f"{rec.path}|{rec.size}|{rec.mtime_ns}".encode()).hexdigest()
    out = os.path.join(thumbs_dir, key[:2], key + ".jpg")
    if os.path.exists(out):
        return out
    try:
        if rec.kind == "image":
            im, _ = load_photo(rec.path, size)
            im = _rgb_for_jpeg(im)
            im.thumbnail((size, size), Image.Resampling.LANCZOS)
        else:
            pm = get_pymupdf()
            doc = pm.open(io_path(rec.path))
            try:
                if doc.needs_pass and not doc.authenticate(""):
                    return None
                page = doc[0]
                zoom = size / max(page.rect.width, page.rect.height, 1)
                pix = page.get_pixmap(matrix=pm.Matrix(zoom, zoom), alpha=False)
                im = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
            finally:
                doc.close()
        os.makedirs(os.path.dirname(out), exist_ok=True)
        tmp = out + f".{os.getpid()}.{threading.get_ident()}.tmp"
        im.save(tmp, "JPEG", quality=80)
        os.replace(tmp, out)
        return out
    except Exception as e:
        log.info("No thumbnail for %s: %s", rec.path, e)
        return None


def make_crop(rec: FileRec, which: str) -> bytes | None:
    tiles = rec.meta.get("tiles")
    if rec.kind != "image" or tiles is None or len(tiles) != 16:
        return None
    idx = int(np.argmax(tiles) if which == "sharp" else np.argmin(tiles))
    im, _ = load_photo(rec.path, None)
    w, h = im.size
    i, j = divmod(idx, 4)
    x0, y0, x1, y1 = j * w // 4, i * h // 4, (j + 1) * w // 4, (i + 1) * h // 4
    cw, ch = min(900, x1 - x0), min(900, y1 - y0)
    cx, cy = (x0 + x1) // 2, (y0 + y1) // 2
    box = (cx - cw // 2, cy - ch // 2, cx - cw // 2 + cw, cy - ch // 2 + ch)
    buf = io.BytesIO()
    _rgb_for_jpeg(im.crop(box)).save(buf, "JPEG", quality=90)
    return buf.getvalue()


def view_bytes(rec: FileRec) -> tuple[bytes | None, str]:
    """Full-size, browser-viewable bytes for a file (HEIC converted to JPEG)."""
    ext = os.path.splitext(rec.path)[1].lower()
    if rec.kind == "pdf":
        with open(io_path(rec.path), "rb") as f:
            return f.read(), "application/pdf"
    if ext in BROWSER_IMAGE_TYPES:
        with open(io_path(rec.path), "rb") as f:
            return f.read(), BROWSER_IMAGE_TYPES[ext]
    im, _ = load_photo(rec.path, 4096)
    buf = io.BytesIO()
    _rgb_for_jpeg(im).save(buf, "JPEG", quality=90)
    return buf.getvalue(), "image/jpeg"


# --------------------------------------------------------------------------
# Scan orchestration
# --------------------------------------------------------------------------

@dataclass
class ScanResult:
    recs: list
    groups: list
    stats: ScanStats
    scan_id: int
    opts: object
    roots: list
    member_group: dict = field(default_factory=dict)   # fid -> gid
    group_by_id: dict = field(default_factory=dict)
    matcher: object = None
    grouper: object = None

    def __post_init__(self):
        for g in self.groups:
            self.group_by_id[g.gid] = g
            for m in g.members:
                self.member_group[m.fid] = g.gid


class Progress:
    def __init__(self, enabled: bool):
        self.enabled = enabled
        self.tty = enabled and sys.stdout.isatty()
        self.last = 0.0
        self.width = 0

    def update(self, text: str):
        if not self.enabled:
            return
        now = time.monotonic()
        if now - self.last < (0.25 if self.tty else 10):
            return
        self.last = now
        if self.tty:
            cols = shutil.get_terminal_size((100, 20)).columns - 1
            line = text[:cols]
            print("\r" + line.ljust(self.width), end="", flush=True)
            self.width = len(line)
        else:
            print(text, flush=True)

    def done(self, text: str):
        if not self.enabled:
            return
        if self.tty:
            print("\r" + text.ljust(self.width), flush=True)
        else:
            print(text, flush=True)
        self.width = 0
        self.last = 0.0


def worker_settings(opts) -> dict:
    return {"max_pages": opts.max_pages, "tesseract": opts.tesseract, "ocr_lang": opts.ocr_lang}


def apply_result(rec: FileRec, res: dict):
    if "file_sha" in res:
        rec.file_sha = res["file_sha"]
    if not any(k in res for k in ("meta", "error", "phash", "page_hashes")):
        return
    rec.analysed = True
    rec.error = res.get("error")
    rec.content_sha = res.get("content_sha")
    mh = res.get("minhash")
    rec.minhash = None if mh is None else np.asarray(mh, dtype=np.uint64)
    rec.phash = res.get("phash")
    rec.page_hashes = res.get("page_hashes")
    rec.taken_at = res.get("taken_at")
    rec.camera = res.get("camera")
    rec.sharpness = res.get("sharpness")
    rec.exposure = res.get("exposure")
    rec.meta = compact_meta(res.get("meta") or {})


def cached_analysis_usable(rec: FileRec, row: dict, opts) -> bool:
    if row["kind"] != rec.kind or row["algo_version"] != ALGO_VERSION:
        return False
    if (row["error"] or "").startswith(WORKER_CRASH):
        return False        # may have been a one-off (e.g. low memory): try again
    if rec.kind == "pdf" and row["meta"]:
        meta = json.loads(row["meta"])
        if not meta.get("encrypted") and row["error"] is None and (
                meta.get("max_pages") != opts.max_pages
                or bool(meta.get("ocr_on")) != bool(opts.tesseract)):
            return False
    return True


def own_folders(cache_dir: str) -> list[str]:
    """DupeFinder's cache and the Python running it (the bundled one included)."""
    home = path_key(os.path.expanduser("~"))
    dirs = [normalize_root(cache_dir)]
    for p in {sys.prefix, sys.base_prefix}:
        p = os.path.abspath(p)
        if os.path.isdir(p) and not is_drive_root(p) and path_key(p) != home \
                and len(Path(p).parts) > 2:
            dirs.append(p)
    return dirs


def run_scan(opts, cache: Cache, show_progress: bool = True) -> ScanResult:
    stats = ScanStats()
    prog = Progress(show_progress)
    scan_id = cache.start_scan(opts.roots, {k: v for k, v in vars(opts).items()
                                            if k not in ("type_set",)})
    walker = Walker(opts, stats, own_dirs=own_folders(cache.dir))
    recs: list[FileRec] = []
    try:
        for rec in walker.walk(opts.roots):
            rec.fid = len(recs)
            recs.append(rec)
            prog.update(f"Scanning folders: {len(recs):,} PDFs and photos found")
    except KeyboardInterrupt:
        stats.interrupted = True
        raise
    prog.done(f"Scanning folders: {len(recs):,} PDFs and photos found")

    rows = cache.lookup([r.path for r in recs])
    size_counts = collections.Counter(r.size for r in recs)
    tasks, reused = [], []
    for r in recs:
        row = rows.get(r.path)
        if row and not opts.rescan and row["size"] == r.size and row["mtime_ns"] == r.mtime_ns:
            Cache.apply_row(r, row, analysis=cached_analysis_usable(r, row, opts))
        need_sha = size_counts[r.size] > 1 and not r.file_sha
        if need_sha or not r.analysed:
            tasks.append((r.fid, "analyse", {"path": r.path, "kind": r.kind,
                                             "sha": need_sha, "analyse": not r.analysed}))
        else:
            reused.append(r.path)
    stats.from_cache = len(reused)
    cache.touch(reused, scan_id)
    del rows                          # cached rows are applied; free them on huge scans

    pool = WorkerPool(opts.workers, worker_settings(opts), opts.timeout)
    try:
        batch, done, last_save = [], 0, time.monotonic()
        for fid, ok, res in pool.run(tasks):
            r = recs[fid]
            if ok:
                apply_result(r, res)
            else:
                r.error, r.analysed = res, True
            done += 1
            batch.append(r)
            if len(batch) >= 200 or time.monotonic() - last_save > 5:
                cache.save(batch, scan_id)
                batch, last_save = [], time.monotonic()
            prog.update(f"Fingerprinting: {done:,} of {len(tasks):,} files")
        cache.save(batch, scan_id)
        stats.analysed = len(tasks)
        prog.done(f"Fingerprinting: {len(tasks):,} files processed, {len(reused):,} unchanged "
                  "files taken from the cache")

        def compute(op: str, fids: list[int]) -> dict:
            out, changed = {}, []
            tasks_ = [(f, op, {"path": recs[f].path}) for f in fids]
            for fid, ok, res in pool.run(tasks_):
                if not ok:
                    log.info("%s failed for %s: %s", op, recs[fid].path, res)
                    continue
                if op == "pixsha":
                    recs[fid].content_sha = res
                    changed.append(recs[fid])
                else:
                    out[fid] = np.frombuffer(res, dtype=np.uint64)
            cache.save(changed, scan_id)
            return out

        if opts.ai:
            compute_embeddings(recs, opts, cache, scan_id, prog)
        prog.done("Matching and grouping...")
        matcher = Matcher(recs, opts, compute, prog.update)
        links = matcher.match_all()
        grouper = Grouper(recs, matcher, opts)
        groups = grouper.build(links)
    finally:
        pool.close()
    matcher.compute = inprocess_compute(recs, worker_settings(opts))

    for r in recs:
        if r.error:
            stats.errors.append((r.path, r.error))
            log.warning("Couldn't read %s: %s", r.path, r.error)
        if r.meta.get("encrypted"):
            stats.protected.append(r.path)
    cache.forget_missing(walker.listed, opts.type_set, scan_id)
    stats.finished = time.time()
    result = ScanResult(recs=recs, groups=groups, stats=stats, scan_id=scan_id,
                        opts=opts, roots=opts.roots, matcher=matcher, grouper=grouper)
    cache.finish_scan(scan_id, {"files": len(recs), "groups": len(groups),
                                "analysed": stats.analysed, "from_cache": stats.from_cache,
                                **stats.counts()})
    return result


def inprocess_compute(recs: list[FileRec], settings: dict):
    """Same contract as run_scan's compute(), without worker processes."""
    def compute(op: str, fids: list[int]) -> dict:
        out = {}
        for f in fids:
            try:
                res = WORKER_OPS[op]({"path": recs[f].path}, settings)
            except Exception as e:
                log.info("%s failed for %s: %s", op, recs[f].path, e)
                continue
            if op == "pixsha":
                recs[f].content_sha = res
            else:
                out[f] = np.frombuffer(res, dtype=np.uint64)
        return out
    return compute


# --------------------------------------------------------------------------
# Optional AI similarity (local model only; never downloads anything)
# --------------------------------------------------------------------------

def load_embedder(opts):
    """Return images -> unit vectors, or None. Tests replace this function."""
    try:
        import open_clip
        import torch
    except ImportError:
        print("AI similarity needs the optional offline AI pack (torch and open_clip_torch); "
              "continuing without it.")
        return None
    model_path = opts.ai_model
    if not model_path or not os.path.isfile(model_path):
        print(f"AI model file not found ({model_path}); continuing without AI similarity.")
        return None
    model, _, preprocess = open_clip.create_model_and_transforms(opts.ai_arch, pretrained=model_path)
    model.eval()

    def embed(images):
        with torch.no_grad():
            x = torch.stack([preprocess(im) for im in images])
            f = model.encode_image(x)
            f = f / f.norm(dim=-1, keepdim=True)
        return f.cpu().numpy().astype(np.float32)

    return embed


def compute_embeddings(recs, opts, cache, scan_id, prog):
    try:
        size = os.path.getsize(opts.ai_model)
    except OSError:
        size = 0
    model_id = f"{opts.ai_arch}|{os.path.basename(opts.ai_model)}|{size}"
    for r in recs:
        if r.embedding is not None and r.meta.get("ai_model") != model_id:
            r.embedding = None          # made by another model: not comparable
    todo = [r for r in recs if r.kind == "image" and r.embedding is None and not r.error]
    if not todo:
        return
    embed = load_embedder(opts)
    if embed is None:
        opts.ai = False
        return

    def decode(r):
        try:
            im, _ = load_photo(r.path, 256)
            return r, _rgb_for_jpeg(im)
        except Exception as e:
            log.info("AI decode failed for %s: %s", r.path, e)
            return r, None

    done = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, opts.workers)) as ex:
        for i in range(0, len(todo), 32):
            pairs = [p for p in ex.map(decode, todo[i:i + 32]) if p[1] is not None]
            if pairs:
                vecs = embed([im for _, im in pairs])
                for (r, _), v in zip(pairs, vecs):
                    r.embedding = np.asarray(v, dtype=np.float32)
                    r.meta["ai_model"] = model_id
                cache.save([r for r, _ in pairs], scan_id)
            done += len(todo[i:i + 32])
            prog.update(f"AI similarity: {done:,} of {len(todo):,} photos")


# --------------------------------------------------------------------------
# Report data, static report and CSV
# --------------------------------------------------------------------------

def member_dict(rec: FileRec, m: Member, g: Group, opts, mode: str, thumbs: dict) -> dict:
    ext = os.path.splitext(rec.path)[1].lower().lstrip(".")
    if rec.kind == "image" and rec.meta.get("taken"):
        date, date_kind = rec.meta["taken"], "taken"
    else:
        try:
            date = dt.datetime.fromtimestamp(rec.mtime_ns / 1e9).strftime("%Y-%m-%d %H:%M:%S")
        except (OSError, OverflowError, ValueError):
            date = "unknown"
        date_kind = "modified"
    dims = (f"{rec.meta['w']} × {rec.meta['h']}"
            if rec.kind == "image" and rec.meta.get("w") else None)
    d = {
        "fid": rec.fid, "name": rec.name, "folder": os.path.dirname(rec.path), "path": rec.path,
        "kind": rec.kind, "ext": ext, "size": rec.size, "dims": dims,
        "pages": rec.meta.get("pages") if rec.kind == "pdf" else None,
        "date": date, "date_kind": date_kind,
        "keeper": m.keeper, "suggest": m.suggest, "label": m.label, "label_kind": m.label_kind,
        "score": m.score, "detail": m.detail,
        "sharpness": None if rec.sharpness is None else round(rec.sharpness, 1),
        "rel_sharpness": m.rel_sharpness,
        "exposure": exposure_text(rec, opts) if rec.kind == "image" else None,
        "flags": m.flags, "locked": m.locked, "lock_reason": m.lock_reason,
        "thumb": None, "file_url": None, "folder_url": None,
        "crops": mode == "server" and len(rec.meta.get("tiles", ())) == 16,
    }
    if rec.error:
        d["detail"] = (d["detail"] + "; " if d["detail"] else "") + f"Couldn't read: {rec.error}"
    if mode == "static":
        t = thumbs.get(rec.fid)
        d["thumb"] = file_uri(t) if t else None
        d["file_url"] = file_uri(rec.path)
        d["folder_url"] = file_uri(os.path.dirname(rec.path))
    return d


def report_data(result: ScanResult, mode: str, token: str = "", thumbs: dict | None = None) -> dict:
    o, s, recs = result.opts, result.stats, result.recs
    thumbs = thumbs or {}
    groups = []
    for g in result.groups:
        groups.append({
            "id": g.gid, "type": g.type, "kind": g.kind, "title": g.title,
            "space": g.space(recs), "min_score": g.min_score(), "burst_shots": g.burst_shots,
            "members": [member_dict(recs[m.fid], m, g, o, mode, thumbs) for m in g.members],
        })
    sharp = [r.sharpness for r in recs if r.kind == "image" and r.sharpness is not None]
    pct = None
    if sharp:
        p = np.percentile(np.asarray(sharp), [10, 50, 90])
        pct = {"p10": round(float(p[0]), 1), "p50": round(float(p[1]), 1), "p90": round(float(p[2]), 1)}
    dup_groups = [g for g in result.groups if g.type != "poor"]
    return {
        "mode": mode, "token": token if mode == "server" else "",
        "generated": dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
        "summary": {
            "files_scanned": len(recs),
            "pdfs": sum(r.kind == "pdf" for r in recs),
            "photos": sum(r.kind == "image" for r in recs),
            "groups": len(dup_groups),
            "bursts": sum(g.type == "burst" for g in dup_groups),
            "poor": sum(g.total or len(g.members) for g in result.groups if g.type == "poor"),
            "space_to_free": sum(g.space(recs) for g in dup_groups),
            "scan_seconds": round((s.finished or time.time()) - s.started, 1),
            "roots": result.roots,
            "settings": {"min_score": o.min_score, "max_bits": o.max_bits,
                         "burst_gap": o.burst_gap, "blur_cutoff": o.blur_cutoff,
                         "dark_cutoff": o.dark_cutoff, "clip_cutoff": o.clip_cutoff},
            "sharpness_percentiles": pct,
        },
        "skipped": {
            "counts": s.counts(),
            "folders": [{"path": p, "reason": why} for p, why in s.denied],
            "errors": [{"path": p, "error": e} for p, e in s.errors],
            "protected": list(s.protected),
        },
        "groups": groups,
    }


def json_for_script(data) -> str:
    text = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    for a, b in (("<", "\\u003c"), (">", "\\u003e"), ("&", "\\u0026"),
                 ("\u2028", "\\u2028"), ("\u2029", "\\u2029")):
        text = text.replace(a, b)
    return text


STATIC_CSP = ("default-src 'none'; img-src file: data:; style-src 'unsafe-inline'; "
              "script-src 'unsafe-inline'; base-uri 'none'; form-action 'none'")
SERVER_CSP = ("default-src 'none'; img-src 'self' data: blob:; style-src 'unsafe-inline'; "
              "script-src 'unsafe-inline'; connect-src 'self'; frame-src 'self'; "
              "base-uri 'none'; form-action 'none'; frame-ancestors 'none'")


def render_report(data: dict) -> str:
    # The data rides in a JSON block: browsers parse big JSON far faster than
    # the same data written as a JavaScript literal (seconds saved on huge scans).
    html = REPORT_TEMPLATE.replace(
        "__DF_DATA__", 'JSON.parse(document.getElementById("df-data").textContent)', 1)
    start = html.index("<body")
    start = html.index(">", start) + 1
    html = (html[:start] + '\n<script id="df-data" type="application/json">'
            + json_for_script(data) + "</script>" + html[start:])
    if data["mode"] == "static":
        html = html.replace("<head>", '<head>\n<meta http-equiv="Content-Security-Policy" '
                                      f'content="{STATIC_CSP}">', 1)
    return html


def write_csv(result: ScanResult, path: str):
    recs, o = result.recs, result.opts
    cols = ["group", "type", "suggestion", "kept_copy", "path", "kind", "size_bytes", "match",
            "score", "detail", "pages", "resolution", "date", "flags"]
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(cols)
        for g in result.groups:
            for m in g.members:
                r = recs[m.fid]
                d = member_dict(r, m, g, o, "csv", {})
                w.writerow([g.gid, g.type, m.suggest, "yes" if m.keeper else "", r.path, r.kind,
                            r.size, m.label, "" if m.score is None else m.score, d["detail"],
                            d["pages"] or "", d["dims"] or "", d["date"], " ".join(m.flags)])


def write_static_report(result: ScanResult, cache: Cache) -> tuple[str, str]:
    reports = os.path.join(cache.dir, "reports")
    os.makedirs(reports, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    html_path = os.path.join(reports, f"report-{stamp}.html")
    csv_path = result.opts.csv or os.path.join(reports, f"report-{stamp}.csv")
    thumbs_dir = os.path.join(cache.dir, "thumbs")
    fids = [m.fid for g in result.groups for m in g.members]
    thumbs = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, result.opts.workers)) as ex:
        for fid, t in zip(fids, ex.map(lambda f: make_thumbnail(result.recs[f], thumbs_dir), fids)):
            if t:
                thumbs[fid] = t
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(render_report(report_data(result, "static", thumbs=thumbs)))
    write_csv(result, csv_path)
    return html_path, csv_path


# --------------------------------------------------------------------------
# Local server: session token, report API and move actions
# --------------------------------------------------------------------------

class ReportServer:
    def __init__(self, result: ScanResult, cache: Cache, port: int = 0):
        self.result = result
        self.cache = cache
        self.token = secrets.token_urlsafe(24)
        self.mover = Mover(result, cache)
        self.thumbs_dir = os.path.join(cache.dir, "thumbs")
        self.last_activity = time.monotonic()
        self.stopped = threading.Event()
        self.requested_port = port
        self.httpd = None
        self.port = 0
        self._html = None

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/?token={self.token}"

    def html(self) -> bytes:
        if self._html is None:
            self._html = render_report(report_data(self.result, "server", self.token)).encode("utf-8")
        return self._html

    def start(self):
        handler = type("Handler", (ReportHandler,), {"app": self})
        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", self.requested_port), handler)
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, name="dupefinder-server",
                         daemon=True).start()
        threading.Thread(target=self._idle_watch, name="dupefinder-idle", daemon=True).start()

    def _idle_watch(self):
        while not self.stopped.wait(15):
            if time.monotonic() - self.last_activity > IDLE_TIMEOUT:
                print("\nNo activity for 30 minutes; stopping the report server.")
                self.stopped.set()

    def wait(self):
        while not self.stopped.wait(0.5):
            pass

    def stop(self):
        self.stopped.set()
        if self.httpd is not None:
            self.httpd.shutdown()
            self.httpd.server_close()
            self.httpd = None


class ReportHandler(http.server.BaseHTTPRequestHandler):
    app: ReportServer = None
    server_version = "DupeFinder"
    sys_version = ""

    def log_message(self, fmt, *args):
        log.debug("http: " + fmt, *args)

    # -- helpers ------------------------------------------------------
    def _send(self, code: int, body: bytes, ctype: str, extra: dict | None = None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, code: int, obj):
        self._send(code, json.dumps(obj).encode("utf-8"), "application/json",
                   {"Cache-Control": "no-store"})

    def _deny(self, code: int, why: str):
        self._json(code, {"ok": False, "reason": why})

    def _allowed(self, header_token: bool) -> bool:
        app = self.app
        port = app.port
        if self.headers.get("Host", "") not in (f"127.0.0.1:{port}", f"localhost:{port}"):
            self._deny(403, "Wrong host")
            return False
        origin = self.headers.get("Origin")
        if origin and origin not in (f"http://127.0.0.1:{port}", f"http://localhost:{port}"):
            self._deny(403, "Requests from other sites are refused")
            return False
        if self.headers.get("Sec-Fetch-Site", "same-origin") not in ("same-origin", "none"):
            self._deny(403, "Requests from other sites are refused")
            return False
        if header_token:
            given = self.headers.get("X-Token", "")
        else:
            q = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
            given = (q.get("token") or [""])[0]
        if not secrets.compare_digest(given.encode(), app.token.encode()):
            self._deny(403, "Missing or wrong session token")
            return False
        app.last_activity = time.monotonic()
        return True

    def _rec(self, fid_text: str) -> FileRec | None:
        try:
            fid = int(fid_text)
        except ValueError:
            return None
        if fid not in self.app.result.member_group:
            return None
        return self.app.result.recs[fid]

    # -- routes -------------------------------------------------------
    def do_OPTIONS(self):
        self._deny(405, "Not allowed")

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        if not self._allowed(header_token=False):
            return
        parts = urllib.parse.urlsplit(self.path).path.strip("/").split("/")
        try:
            if parts == [""]:
                self._send(200, self.app.html(), "text/html; charset=utf-8",
                           {"Cache-Control": "no-store", "Content-Security-Policy": SERVER_CSP})
            elif parts[0] == "thumb" and len(parts) == 2:
                rec = self._rec(parts[1])
                t = rec and make_thumbnail(rec, self.app.thumbs_dir)
                if not t:
                    return self._deny(404, "No thumbnail")
                with open(t, "rb") as f:
                    self._send(200, f.read(), "image/jpeg", {"Cache-Control": "private, max-age=3600"})
            elif parts[0] == "view" and len(parts) == 2:
                rec = self._rec(parts[1])
                if not rec:
                    return self._deny(404, "Unknown file")
                body, ctype = view_bytes(rec)
                self._send(200, body, ctype, {"Cache-Control": "no-store",
                                              "Content-Disposition": "inline"})
            elif parts[0] == "crop" and len(parts) == 3 and parts[2] in ("sharp", "blur"):
                rec = self._rec(parts[1])
                body = rec and make_crop(rec, parts[2])
                if not body:
                    return self._deny(404, "No crop")
                self._send(200, body, "image/jpeg", {"Cache-Control": "private, max-age=3600"})
            else:
                self._deny(404, "Not found")
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            log.warning("GET %s failed: %s", self.path, e)
            try:
                self._deny(500, describe_error(e))
            except Exception:
                pass

    def do_POST(self):
        if not self._allowed(header_token=True):
            return
        if "application/json" not in self.headers.get("Content-Type", ""):
            return self._deny(415, "JSON expected")
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = -1
        if not 0 <= length <= 1 << 20:
            return self._deny(413, "Request too large")
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            return self._deny(400, "Bad JSON")
        route = urllib.parse.urlsplit(self.path).path
        try:
            if route == "/api/move":
                ids = body.get("ids")
                if not isinstance(ids, list):
                    return self._deny(400, "ids must be a list")
                self._json(200, {"results": self.app.mover.move(ids)})
            elif route == "/api/open":
                rec = self._rec(str(body.get("fid")))
                if not rec:
                    return self._deny(404, "Unknown file")
                if rec.fid in self.app.mover.moved:
                    return self._json(200, {"ok": False, "reason": "The file was moved"})
                open_in_system(rec.path, bool(body.get("reveal")))
                self._json(200, {"ok": True})
            elif route == "/api/quit":
                self._json(200, {"ok": True})
                self.app.stopped.set()
            else:
                self._deny(404, "Not found")
        except Exception as e:
            log.warning("POST %s failed: %s", route, e)
            self._deny(500, describe_error(e))


# --------------------------------------------------------------------------
# Command line
# --------------------------------------------------------------------------

EPILOG = r"""
examples:
  python dupefinder.py C:\ D:\
  python dupefinder.py D:\Photos --types image --ai
  python dupefinder.py D:\Docs --types pdf --min-score 90 --report-only
  python dupefinder.py --undo-review

Nothing is deleted permanently: chosen files go to the Recycle Bin, or to a
_duplicates_review folder on USB and network drives. The tool works offline.
"""


def build_parser() -> argparse.ArgumentParser:
    cpu = max(1, (os.cpu_count() or 2) - 1)
    p = argparse.ArgumentParser(
        prog="dupefinder", description="Find duplicate and near-duplicate PDFs and photos by content.",
        epilog=EPILOG, formatter_class=argparse.RawDescriptionHelpFormatter)
    a = p.add_argument
    a("roots", nargs="*", metavar="ROOTS", help="folders or drives to scan")
    a("--types", default="pdf,image", help="limit the scan to pdf or image (default: pdf,image)")
    a("--exclude", action="append", default=[], metavar="PATH",
      help="another folder to skip; repeatable")
    a("--include-system", action="store_true",
      help="also scan system and program folders; their files are never pre-selected")
    a("--min-score", type=float, default=85.0,
      help="lowest %% reported for text and scanned-PDF matches (default: 85)")
    a("--max-bits", type=int, default=8,
      help="most differing bits, out of 64, for a photo visual match (default: 8)")
    a("--burst-gap", type=float, default=3.0, help="most seconds between shots in a burst (default: 3)")
    a("--blur-cutoff", type=float, default=DEFAULT_BLUR_CUTOFF,
      help=f"sharpness score (0-100) below which a photo is flagged blurry "
           f"(default: {DEFAULT_BLUR_CUTOFF:g}); the report shows your photos' range to help tune it")
    a("--dark-cutoff", type=float, default=40.0,
      help="median brightness (0-255) below which a photo is flagged dark (default: 40)")
    a("--clip-cutoff", type=float, default=25.0,
      help="%% of pixels near pure black or white above which a photo is flagged (default: 25)")
    a("--quick", action="store_true",
      help="skip the search for turned, mirrored and cropped photo copies (faster on huge libraries)")
    a("--ocr", action="store_true", help="read scanned PDFs with a locally installed Tesseract")
    a("--ocr-lang", default="eng", help="Tesseract language(s), e.g. eng+deu (default: eng)")
    a("--max-pages", type=int, default=30, help="pages rendered per scanned PDF (default: 30)")
    a("--ai", action="store_true",
      help="AI similarity for bursts with movement, crops and heavy edits (local model only)")
    a("--ai-model", metavar="PATH", help="local CLIP model weights file (default: <cache>/models/clip.pt)")
    a("--ai-arch", default="ViT-B-32", help="open_clip architecture of --ai-model (default: ViT-B-32)")
    a("--ai-min-score", type=float, default=92.0, help="lowest AI similarity %% reported (default: 92)")
    a("--ai-burst-score", type=float, default=85.0,
      help="AI similarity %% that keeps a shot in a burst (default: 85)")
    a("--keep", default="auto",
      choices=["auto", "newest", "oldest", "largest", "highest-res", "shortest-path"],
      help="keep rule (default: auto)")
    a("--prefer", action="append", default=[], metavar="PATH",
      help="prefer keeping files under this folder; repeatable")
    a("--workers", type=int, default=cpu, help=f"worker processes (default: {cpu})")
    a("--timeout", type=float, default=60.0, help="most seconds spent on one file (default: 60)")
    a("--cache", metavar="PATH", help=f"cache, thumbnails and logs (default: {default_cache_dir()})")
    a("--rescan", action="store_true", help="ignore cached fingerprints")
    a("--report-only", action="store_true", help="write a static HTML report and CSV; no server, no trash")
    a("--csv", metavar="PATH", help="also export all groups to CSV")
    a("--port", type=int, default=0, help="port for the local server (default: random)")
    a("--no-browser", action="store_true", help="don't open the browser automatically")
    a("--pick", action="store_true",
      help="choose the folders to scan in a window (run.bat does this without arguments)")
    a("--undo-review", action="store_true",
      help="move files from _duplicates_review folders back, using the log")
    a("--version", action="version", version=f"%(prog)s {__version__}")
    return p


def split_paths(text: str) -> list[str]:
    r"""Split typed paths; quotes allow spaces: C:\ "D:\My Photos"."""
    import shlex
    try:
        parts = shlex.split(text, posix=False)
    except ValueError:
        parts = text.split()
    return [p.strip('"').strip("'") for p in parts if p.strip('"').strip("'")]


def available_drives() -> list[str]:
    if not IS_WINDOWS:
        return []
    import string
    return [f"{d}:\\" for d in string.ascii_uppercase if os.path.exists(f"{d}:\\")]


def pick_folders(types: set[str], on_ready=None):
    """Window for choosing folders/drives and file types (Python's built-in Tk).

    Returns (folders, types), or None if cancelled. `on_ready(api)` is for tests.
    """
    import tkinter as tk
    from tkinter import filedialog, messagebox
    if IS_WINDOWS:
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass
    root = tk.Tk()
    root.title("DupeFinder - choose what to scan")
    root.minsize(560, 380)
    folders: list[str] = []
    result: dict = {}
    pad = {"padx": 10, "pady": 4}

    tk.Label(root, text="Folders and drives to scan", font=("Segoe UI", 11, "bold"),
             anchor="w").pack(fill="x", **pad)
    box = tk.Listbox(root, selectmode=tk.EXTENDED, height=9, activestyle="none")
    box.pack(fill="both", expand=True, padx=10)

    def add(path: str):
        path = os.path.normpath(path)
        if path and path not in folders:
            folders.append(path)
            box.insert(tk.END, path)

    def add_folder():
        path = filedialog.askdirectory(parent=root, mustexist=True,
                                       title="Choose a folder or drive to scan")
        if path:
            add(path)

    def remove():
        for i in reversed(box.curselection()):
            box.delete(i)
            del folders[i]

    row = tk.Frame(root)
    row.pack(fill="x", **pad)
    tk.Button(row, text="Add folder...", command=add_folder).pack(side="left")
    tk.Button(row, text="Remove", command=remove).pack(side="left", padx=6)
    drives = available_drives()
    if drives:
        tk.Label(row, text="   Whole drive:").pack(side="left")
        for d in drives:
            tk.Button(row, text=d, command=lambda d=d: add(d)).pack(side="left", padx=2)

    opts_row = tk.Frame(root)
    opts_row.pack(fill="x", **pad)
    want_pdf = tk.BooleanVar(value="pdf" in types)
    want_img = tk.BooleanVar(value="image" in types)
    tk.Label(opts_row, text="Look for:").pack(side="left")
    tk.Checkbutton(opts_row, text="PDFs", variable=want_pdf).pack(side="left", padx=6)
    tk.Checkbutton(opts_row, text="Photos (JPG, PNG, HEIC, WebP, TIFF, RAW...)", variable=want_img).pack(side="left")
    tk.Label(root, text="System and program folders are skipped. Nothing is moved until "
                        "you confirm it in the report.", fg="#555", anchor="w",
             justify="left", wraplength=520).pack(fill="x", **pad)

    def scan():
        chosen = {t for t, v in (("pdf", want_pdf), ("image", want_img)) if v.get()}
        if not folders:
            messagebox.showinfo("DupeFinder", "Add at least one folder or drive.", parent=root)
            return
        if not chosen:
            messagebox.showinfo("DupeFinder", "Tick PDFs, Photos or both.", parent=root)
            return
        result["value"] = (list(folders), chosen)
        root.destroy()

    bottom = tk.Frame(root)
    bottom.pack(fill="x", padx=10, pady=10)
    tk.Button(bottom, text="Cancel", width=10, command=root.destroy).pack(side="right")
    tk.Button(bottom, text="Scan", width=10, default="active", command=scan).pack(side="right", padx=6)
    root.bind("<Return>", lambda e: scan())
    root.bind("<Escape>", lambda e: root.destroy())
    if on_ready:
        root.after(50, lambda: on_ready({"root": root, "add": add, "scan": scan,
                                         "pdf": want_pdf, "image": want_img}))
    root.mainloop()
    return result.get("value")


def choose_roots(types: set[str]):
    """Folder window, or a typed prompt if a window can't be shown."""
    try:
        return pick_folders(types)
    except Exception as e:          # no Tk installed, or no desktop to show it on
        log.info("Folder window unavailable: %s", e)
    text = input("Folders or drives to scan (for example C:\\ \"D:\\My Photos\"): ")
    folders = split_paths(text)
    return (folders, types) if folders else None


def normalize_root(root: str) -> str:
    if IS_WINDOWS and re.fullmatch(r"[A-Za-z]:", root.strip()):
        root = root.strip() + "\\"
    root = os.path.abspath(os.path.expanduser(root))
    if IS_WINDOWS and os.path.isdir(root):
        try:
            real = os.path.realpath(root)
            if not real.startswith("\\\\?\\"):
                root = real      # the real letter case, so cache keys stay stable
        except OSError:
            pass
    return root


def parse_args(argv=None):
    p = build_parser()
    opts = p.parse_args(argv)
    opts.type_set = {t.strip().lower() for t in opts.types.split(",") if t.strip()}
    opts.type_set = {"image" if t in ("images", "photo", "photos") else
                     "pdf" if t == "pdfs" else t for t in opts.type_set}
    if not opts.type_set or not opts.type_set <= {"pdf", "image"}:
        p.error("--types takes pdf, image or pdf,image")
    if not opts.undo_review and not opts.pick and not opts.roots:
        p.error("give at least one folder or drive to scan, e.g. C:\\ D:\\")
    if not 0 <= opts.max_bits <= 32:
        p.error("--max-bits must be between 0 and 32")
    if opts.max_pages < 1:
        p.error("--max-pages must be at least 1")
    opts.cache = os.path.abspath(opts.cache or default_cache_dir())
    roots = []
    for r in opts.roots:
        root = normalize_root(r)
        if not os.path.isdir(root):
            p.error(f"not a folder: {r}")
        roots.append(root)
    keys = sorted({path_key(r): r for r in roots}.items(), key=lambda kv: len(kv[0]))
    kept = []
    for _k, r in keys:   # drop roots nested inside another root
        if not any(is_under(r, path_key(x)) for x in kept):
            kept.append(r)
    opts.roots = kept
    opts.exclude = [normalize_root(x) for x in opts.exclude]
    opts.prefer = [normalize_root(x) for x in opts.prefer]
    opts.workers = max(1, opts.workers)
    if not opts.ai_model:
        opts.ai_model = os.path.join(opts.cache, "models", "clip.pt")
    opts.tesseract = find_tesseract() if opts.ocr else None
    return opts


def setup_logging(cache_dir: str) -> str:
    logs = os.path.join(cache_dir, "logs")
    os.makedirs(logs, exist_ok=True)
    old = sorted(f for f in os.listdir(logs) if f.startswith("dupefinder-") and f.endswith(".log"))
    for f in old[:-19]:
        try:
            os.remove(os.path.join(logs, f))
        except OSError:
            pass
    path = os.path.join(logs, f"dupefinder-{dt.datetime.now():%Y%m%d-%H%M%S}.log")
    handler = logging.FileHandler(path, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    log.handlers[:] = [handler]
    log.setLevel(logging.INFO)
    log.propagate = False
    return path


def print_summary(result: ScanResult):
    s, recs = result.stats, result.recs
    dup = [g for g in result.groups if g.type != "poor"]
    poor = sum(g.total or len(g.members) for g in result.groups if g.type == "poor")
    space = sum(g.space(recs) for g in dup)
    print(f"\n{len(recs):,} files scanned in {s.finished - s.started:.0f} s: "
          f"{len(dup):,} duplicate groups ({sum(g.type == 'burst' for g in dup)} bursts), "
          f"{poor:,} possibly poor shots, {human_size(space)} could be freed.")
    for label, n in s.counts().items():
        print(f"  {label}: {n:,}")


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(errors="replace")
            except Exception:
                pass
    opts = parse_args(argv)
    if opts.pick:
        picked = choose_roots(opts.type_set)
        if not picked:
            print("Nothing chosen to scan.")
            return 1
        folders, types = picked
        rest = [a for a in (sys.argv[1:] if argv is None else argv) if a != "--pick"]
        opts = parse_args(rest + folders + ["--types", ",".join(sorted(types))])
    install_network_guard()
    os.makedirs(opts.cache, exist_ok=True)
    log_path = setup_logging(opts.cache)
    cache = Cache(opts.cache)
    try:
        if opts.undo_review:
            return undo_review(cache)
        missing = check_dependencies(opts.type_set, need_trash=not opts.report_only)
        if missing:
            print("Missing packages: " + ", ".join(missing))
            print("Run install.bat from the DupeFinder folder (works offline), or:\n"
                  "  py -m pip install --no-index --find-links wheels -r requirements.txt")
            return 2
        if Image is not None:
            Image.MAX_IMAGE_PIXELS = 300_000_000
        if opts.ocr:
            if not opts.tesseract:
                print("--ocr: Tesseract isn't installed, so scanned PDFs are compared by page images only.")
        print(f"{APP_NAME} {__version__}: scanning {', '.join(opts.roots)}")
        try:
            result = run_scan(opts, cache)
        except KeyboardInterrupt:
            print("\nStopped. Nothing was moved.")
            return 130
        except RuntimeError as e:
            print(f"\n{e}")
            return 1
        print_summary(result)
        print(f"Log: {log_path}")
        if opts.csv and not opts.report_only:
            write_csv(result, opts.csv)
            print(f"CSV: {opts.csv}")
        if opts.report_only:
            html_path, csv_path = write_static_report(result, cache)
            print(f"Report: {html_path}\nCSV: {csv_path}")
            if not opts.no_browser:
                webbrowser.open(file_uri(html_path))
            return 0
        server = ReportServer(result, cache, opts.port)
        server.start()
        print(f"\nReport: {server.url}")
        print("Keep this window open while you review. Close the report with its Quit button, "
              "or press Ctrl+C here.")
        if not opts.no_browser:
            webbrowser.open(server.url)
        try:
            server.wait()
        except KeyboardInterrupt:
            pass
        finally:
            server.stop()
            with server.mover.lock:        # let a running move batch finish logging
                path = cache.export_actions()
            if server.mover.moved:
                print(f"\nMoved {len(server.mover.moved)} file(s). Log of every move: {path}")
        return 0
    finally:
        cache.close()
        for h in list(log.handlers):
            log.removeHandler(h)
            h.close()


# --------------------------------------------------------------------------
# Browser report page (inline HTML, CSS and JS; loads nothing from the web)
# --------------------------------------------------------------------------

REPORT_TEMPLATE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light dark">
<meta name="referrer" content="no-referrer">
<meta http-equiv="Content-Security-Policy" content="default-src 'self'; img-src 'self' data: blob: file:; style-src 'unsafe-inline'; script-src 'unsafe-inline'; connect-src 'self'; object-src 'none'; frame-src 'none'; base-uri 'none'">
<title>DupeFinder report</title>
<style>
:root{
  --bg:#f4f5f7;--surface:#fff;--surface2:#f0f2f5;--border:#dde1e7;--border2:#c5ccd5;
  --text:#1b1f24;--muted:#5a636e;--faint:#8b939d;
  --accent:#2559c9;--accent-ink:#fff;--accent-soft:#e6edfb;
  --ok:#1b7a47;--ok-soft:#e3f3ea;--on-danger:#fff;--danger:#bf3329;--danger-soft:#fbe9e7;--warn:#9a5b00;--warn-soft:#fbf0dc;
  --h-ind:#4651c4;--h-teal:#0b7d7d;--h-blue:#2559c9;--h-vio:#7445c2;--h-amb:#9a5b00;--h-mag:#a73a83;--h-grey:#5a636e;--h-red:#bf3329;--h-green:#1b7a47;
  --shadow:0 1px 2px rgba(16,24,40,.05),0 1px 3px rgba(16,24,40,.07);
  --r:10px;
}
@media (prefers-color-scheme:dark){:root{
  --bg:#101317;--surface:#181c21;--surface2:#1f242a;--border:#2a3038;--border2:#3a424c;
  --text:#e5e8ec;--muted:#9ba4ae;--faint:#6f7883;
  --accent:#6f9cff;--accent-ink:#0b1220;--accent-soft:#1d2a44;
  --ok:#52c48a;--ok-soft:#15301f;--on-danger:#2b0d0a;--danger:#ff7d70;--danger-soft:#3a1d1a;--warn:#e2ad4b;--warn-soft:#33270f;
  --h-ind:#9aa3ff;--h-teal:#4cc9c4;--h-blue:#6f9cff;--h-vio:#b996ff;--h-amb:#e2ad4b;--h-mag:#f08ccf;--h-grey:#a3acb6;--h-red:#ff7d70;--h-green:#52c48a;
  --shadow:0 1px 2px rgba(0,0,0,.4);
}}
*,*::before,*::after{box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--bg);color:var(--text);font:14px/1.45 system-ui,-apple-system,"Segoe UI",Roboto,"Helvetica Neue",Arial,sans-serif;overflow-wrap:break-word}
[hidden]{display:none!important}
button,input,select{font:inherit;color:inherit}
:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
.ic{width:16px;height:16px;fill:none;stroke:currentColor;stroke-width:2;stroke-linecap:round;stroke-linejoin:round;flex:none;vertical-align:-3px}
.wrap{max-width:1180px;margin:0 auto;padding:0 16px}
.vh{position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0 0 0 0);white-space:nowrap}
.muted{color:var(--muted)}
/* header */
.top{background:var(--surface);border-bottom:1px solid var(--border)}
.top .wrap{display:flex;align-items:center;gap:12px;padding-top:12px;padding-bottom:12px}
.logo{width:30px;height:30px;color:var(--accent);stroke-width:1.8}
h1{font-size:18px;margin:0;font-weight:650;letter-spacing:-.01em;display:flex;align-items:center;gap:8px;flex-wrap:wrap}
.hsub{color:var(--muted);font-size:12.5px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.grow{flex:1 1 auto;min-width:0}
.pill{font-size:11.5px;font-weight:600;padding:2px 8px;border-radius:99px;background:var(--surface2);color:var(--muted);border:1px solid var(--border)}
/* buttons */
.btn{display:inline-flex;align-items:center;justify-content:center;gap:6px;border:1px solid var(--border2);background:var(--surface);color:var(--text);border-radius:8px;padding:6px 12px;cursor:pointer;text-decoration:none;white-space:nowrap;line-height:1.3}
.btn:hover:not(:disabled){background:var(--surface2)}
.btn:disabled,.btn.off{opacity:.5;cursor:not-allowed}
.btn.sm{padding:4px 10px;font-size:13px}
.btn.pri{background:var(--accent);border-color:var(--accent);color:var(--accent-ink);font-weight:600}
.btn.pri:hover:not(:disabled){filter:brightness(1.08);background:var(--accent)}
.btn.quiet{border-color:transparent;background:transparent}
/* summary */
main{padding:16px 0 24px}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:1px;background:var(--border);border:1px solid var(--border);border-radius:var(--r);overflow:hidden;box-shadow:var(--shadow)}
.stat{background:var(--surface);padding:11px 14px;min-width:0}
.stat .k{font-size:12px;color:var(--muted)}
.stat .v{font-size:20px;font-weight:650;font-variant-numeric:tabular-nums;letter-spacing:-.01em}
.stat .s{font-size:12px;color:var(--muted)}
.stat.hi .v{color:var(--accent)}
.below{display:flex;flex-wrap:wrap;gap:6px 16px;align-items:flex-start;margin:10px 2px 0;font-size:12.5px;color:var(--muted)}
details.skip{flex:1 1 100%}
details.skip>summary{cursor:pointer;display:inline-flex;gap:6px;align-items:center;color:var(--text);font-weight:550}
details.skip>summary::marker{color:var(--muted)}
.skipbox{margin-top:8px;background:var(--surface);border:1px solid var(--border);border-radius:var(--r);padding:10px 14px;display:grid;gap:12px}
.skipbox h3{font-size:12.5px;margin:0 0 4px;color:var(--text)}
.kv{display:grid;grid-template-columns:minmax(0,1fr) auto;gap:2px 16px;max-width:520px}
.kv b{font-weight:600;font-variant-numeric:tabular-nums;text-align:right;color:var(--text)}
.plist{list-style:none;margin:0;padding:0;display:grid;grid-template-columns:minmax(0,2fr) minmax(0,1fr);gap:3px 16px}
.plist li{display:contents}
.plist li.muted{display:block;grid-column:1/-1}
.plist .p{white-space:nowrap;overflow:hidden;text-overflow:ellipsis;color:var(--text)}
.plist .why{color:var(--muted)}
.tune{flex:1 1 100%}
/* filters */
.filters{margin:14px 0 12px;background:var(--surface);border:1px solid var(--border);border-radius:var(--r);padding:12px 14px;display:grid;gap:10px;box-shadow:var(--shadow)}
.frow{display:flex;flex-wrap:wrap;gap:8px 14px;align-items:center}
.flabel{font-size:12px;color:var(--muted);font-weight:550}
.seg{display:inline-flex;border:1px solid var(--border2);border-radius:8px;overflow:hidden;flex:none}
.seg button{border:0;background:transparent;padding:5px 12px;cursor:pointer;color:var(--muted)}
.seg button+button{border-left:1px solid var(--border2)}
.seg button[aria-pressed=true]{background:var(--accent-soft);color:var(--accent);font-weight:600}
.chips{display:flex;flex-wrap:wrap;gap:6px}
.chip{border:1px solid var(--border2);background:var(--surface);border-radius:99px;padding:3px 11px;cursor:pointer;font-size:13px;color:var(--muted)}
.chip span{font-variant-numeric:tabular-nums;color:var(--faint);margin-left:4px}
.chip[aria-pressed=true]{background:var(--accent-soft);border-color:var(--accent);color:var(--accent);font-weight:600}
.chip[aria-pressed=true] span{color:inherit}
.fld{display:flex;align-items:center;gap:6px}
.fld input,.fld select{border:1px solid var(--border2);background:var(--surface);border-radius:8px;padding:5px 8px;min-width:0}
#f-min{width:76px}
#f-folder{width:220px}
.showing{margin-left:auto;font-size:13px;color:var(--muted);font-variant-numeric:tabular-nums}
#f-reset{color:var(--accent);padding:4px 6px}
.bulk{border-top:1px solid var(--border);padding-top:10px}
.ro{font-size:13px;color:var(--muted)}
/* cards */
.card{background:var(--surface);border:1px solid var(--border);border-radius:var(--r);margin:0 0 12px;box-shadow:var(--shadow);overflow:hidden}
.ghead{padding:10px 14px;border-bottom:1px solid var(--border);display:grid;gap:3px}
.gtop{display:flex;flex-wrap:wrap;align-items:center;gap:4px 10px}
.gnum{font-size:14px;margin:0;font-weight:650}
.gk{font-size:12.5px;color:var(--muted)}
.gspace{margin-left:auto;font-size:13px;color:var(--muted);white-space:nowrap}
.gspace b{color:var(--text);font-variant-numeric:tabular-nums}
.gfreed{font-size:12.5px;color:var(--ok);font-weight:600}
.gtitle{font-size:13px;color:var(--muted);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.badge{--c:var(--h-grey);display:inline-flex;align-items:center;gap:4px;font-size:12px;line-height:1.5;padding:0 7px;border-radius:5px;white-space:nowrap;color:var(--c);background:color-mix(in srgb,var(--c) 12%,transparent);border:1px solid color-mix(in srgb,var(--c) 30%,transparent);font-weight:550;max-width:100%;overflow:hidden;text-overflow:ellipsis}
.badge .ic{width:13px;height:13px;vertical-align:0}
.tb{font-size:12px;font-weight:600}
.tb-identical{--c:var(--h-ind)}.tb-same{--c:var(--h-teal)}.tb-similar{--c:var(--h-blue)}.tb-burst{--c:var(--h-amb)}.tb-related{--c:var(--h-grey)}.tb-poor{--c:var(--h-red)}
.lk-identical{--c:var(--h-ind)}.lk-same_text,.lk-same_pixels{--c:var(--h-teal)}.lk-similar_text{--c:var(--h-blue)}.lk-visual,.lk-visual_pdf{--c:var(--h-vio)}.lk-burst{--c:var(--h-amb)}.lk-ai{--c:var(--h-mag)}.lk-related{--c:var(--h-grey)}.lk-via{--c:var(--h-grey);border-style:dashed}.lk-poor{--c:var(--h-red)}
.lk-keeper{background:var(--ok);border-color:var(--ok);color:var(--surface);font-weight:650}
.fl-blurry,.fl-dark,.fl-clipped{--c:var(--h-amb)}.fl-error{--c:var(--h-red)}.fl-prefer{--c:var(--h-blue)}
.sugg-k{--c:var(--h-green)}.sugg-t{--c:var(--h-red)}
.rows{display:block}
.row{display:grid;grid-template-columns:64px minmax(0,1fr) auto;gap:6px 12px;padding:10px 14px;border-top:1px solid var(--border);align-items:center;border-left:3px solid transparent}
.row:first-child{border-top:0}
.row.trash{border-left-color:var(--danger);background:color-mix(in srgb,var(--danger-soft) 55%,transparent)}
.row.moved{opacity:.6;background:var(--surface2);border-left-color:var(--faint)}
.row.moved .seg{display:none}
.thumb{width:64px;height:64px;border-radius:6px;border:1px solid var(--border);background:var(--surface2);padding:0;overflow:hidden;cursor:zoom-in;display:grid;place-items:center;color:var(--faint);text-decoration:none;font-size:11px;font-weight:700;letter-spacing:.04em}
a.thumb{cursor:pointer}
.thumb img{width:100%;height:100%;object-fit:cover;display:block}
.thumb .ph{display:grid;justify-items:center;gap:2px}
.thumb .ph .ic{width:22px;height:22px;stroke-width:1.5}
.info{min-width:0}
.mt{display:flex;min-width:0;white-space:pre}
.mt .h{overflow:hidden;text-overflow:ellipsis;flex:0 1 auto;min-width:0}
.mt .t{flex:0 0 auto;max-width:75%;overflow:hidden;text-overflow:ellipsis}
.name{font-weight:600}
.folder{font-size:12.5px;color:var(--muted)}
.line{display:flex;flex-wrap:wrap;gap:4px 8px;align-items:center;margin-top:4px;font-size:12.5px;color:var(--muted)}
.metaw{overflow:hidden;min-width:0}
.metas{display:flex;flex-wrap:wrap;margin-left:-13px}
.meta{font-variant-numeric:tabular-nums;white-space:nowrap}
.meta::before{content:"\00B7";display:inline-block;width:13px;text-align:center}
.detail,.lock,.note,.qual{font-size:12.5px;margin-top:3px;color:var(--muted)}
.lock{display:flex;gap:5px;align-items:flex-start}
.lock .ic{width:14px;height:14px;margin-top:2px}
.qual{display:flex;flex-wrap:wrap;gap:4px 12px;align-items:center}
.sbar{width:84px;height:6px;border-radius:3px;background:var(--surface2);border:1px solid var(--border);overflow:hidden;display:inline-block;vertical-align:middle;margin-right:6px}
.sbar i{display:block;height:100%;background:var(--ok)}
.sbar.lo i{background:var(--warn)}
.expw{color:var(--warn);font-weight:550}
.note.ok{color:var(--ok);font-weight:600}
.note.err,.note.warn{color:var(--danger);font-weight:600}
.actions{display:flex;flex-wrap:wrap;gap:6px;align-items:center;justify-content:flex-end}
.tg button{padding:4px 12px;font-size:13px;min-width:54px}
.tg button[aria-pressed=true][data-choice=keep]{background:var(--ok-soft);color:var(--ok)}
.tg button[aria-pressed=true][data-choice=trash]{background:var(--danger);color:var(--on-danger);font-weight:600}
.tg button[aria-pressed=true][data-choice=keep]{font-weight:600}
.tg button:disabled{opacity:.45;cursor:not-allowed}
.more{margin:8px 14px 12px}
.sentinel{text-align:center;padding:8px 0 16px}
.empty{text-align:center;padding:40px 16px;color:var(--muted);background:var(--surface);border:1px dashed var(--border2);border-radius:var(--r)}
/* action bar */
.bar{position:sticky;bottom:0;z-index:5;background:color-mix(in srgb,var(--surface) 94%,transparent);backdrop-filter:blur(8px);border-top:1px solid var(--border);box-shadow:0 -2px 10px rgba(0,0,0,.06)}
.bar .wrap{display:flex;flex-wrap:wrap;gap:8px 16px;align-items:center;justify-content:flex-end;padding-top:10px;padding-bottom:10px}
.barinfo{margin-right:auto;font-size:13px;color:var(--muted)}
#move{padding:8px 16px;white-space:normal;text-align:center}
#move.busy{cursor:progress}
/* toasts */
.toasts{position:fixed;right:16px;bottom:76px;z-index:20;display:grid;gap:8px;max-width:min(420px,calc(100vw - 32px))}
.toast{display:flex;gap:10px;align-items:flex-start;background:var(--surface);border:1px solid var(--border2);border-left:4px solid var(--ok);border-radius:8px;padding:10px 10px 10px 12px;box-shadow:0 6px 24px rgba(0,0,0,.18);font-size:13.5px}
.toast.err{border-left-color:var(--danger)}.toast.warn{border-left-color:var(--warn)}
.toast span{flex:1}
.toast .x{border:0;background:transparent;cursor:pointer;color:var(--muted);padding:2px}
/* dialogs */
dialog{border:1px solid var(--border2);border-radius:12px;background:var(--surface);color:var(--text);padding:0;box-shadow:0 20px 60px rgba(0,0,0,.35)}
dialog::backdrop{background:rgba(8,10,14,.62)}
.ask{padding:18px 20px;width:min(460px,calc(100vw - 32px))}
.ask h2{font-size:17px;margin:0 0 10px}
.ask p{margin:0 0 8px}
.ask p.warn{color:var(--danger);font-weight:600}
.askb{display:flex;justify-content:flex-end;gap:8px;margin-top:16px;flex-wrap:wrap}
#lb{width:min(1240px,calc(100vw - 24px));height:calc(100vh - 24px);max-width:none;max-height:none}
.lb{display:flex;flex-direction:column;height:100%}
.lbh{display:flex;gap:10px;align-items:center;padding:10px 12px;border-bottom:1px solid var(--border)}
.lbn{font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.lbs{font-size:12.5px;color:var(--muted);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.lbh .btns{display:flex;gap:6px;flex:none;align-items:center}
.lbc{font-size:12.5px;color:var(--muted);font-variant-numeric:tabular-nums}
.lbb{flex:1;min-height:0;display:grid;grid-template-columns:minmax(0,1fr);gap:12px;padding:12px}
.lbb.wc{grid-template-columns:minmax(0,1fr) 300px}
.lbm{min-height:0;display:grid;place-items:center;background:var(--surface2);border-radius:8px;overflow:hidden}
.lbm img{max-width:100%;max-height:100%;object-fit:contain;display:block}
.lbnote{padding:24px;text-align:center;color:var(--muted);max-width:44ch}
.crops{display:grid;gap:12px;align-content:start;min-height:0;overflow:auto}
.crops figure{margin:0}
.crops figcaption{font-size:12.5px;font-weight:600;margin-bottom:4px}
.crop{aspect-ratio:1;background:var(--surface2);border-radius:8px;overflow:hidden;display:grid;place-items:center;color:var(--muted);font-size:12.5px}
.crop img{width:100%;height:100%;object-fit:none;display:block}
.stopped{position:fixed;inset:0;z-index:50;background:var(--bg);display:grid;place-items:center;padding:24px;text-align:center}
.stopped div{max-width:420px}
.stopped h2{margin:10px 0 6px;font-size:20px}
@media (max-width:720px){
  .row{grid-template-columns:56px minmax(0,1fr);padding:10px 12px}
  .thumb{width:56px;height:56px}
  .actions{grid-column:1/-1;justify-content:flex-start}
  .gspace{margin-left:0}
  #f-folder{width:100%}
  .fld.ff{flex:1 1 100%}
  .fld.ff input{flex:1}
  .showing{margin-left:0;flex:1 1 100%}
  .hsub{white-space:normal}
  .lbb.wc{grid-template-columns:minmax(0,1fr);grid-template-rows:minmax(0,1fr) auto}
  .lbb.wc .crops{grid-template-columns:1fr 1fr}
  #lb{width:100vw;height:100vh;border-radius:0;border:0}
  .lbh{flex-wrap:wrap}
  .lbh .btns{margin-left:auto}
  .bar .wrap{justify-content:stretch}
  #move{flex:1 1 100%}
  .toasts{left:16px;right:16px;bottom:120px;max-width:none}
}
@media (prefers-reduced-motion:no-preference){.btn,.chip,.seg button{transition:background-color .12s,border-color .12s}}
</style>
</head>
<body>
<svg width="0" height="0" style="position:absolute" aria-hidden="true">
 <symbol id="i-logo" viewBox="0 0 24 24"><rect x="3" y="6" width="12" height="15" rx="2"/><path d="M9 6V5a2 2 0 0 1 2-2h8a2 2 0 0 1 2 2v11a2 2 0 0 1-2 2h-4"/><path d="M6 11h6M6 15h4"/></symbol>
 <symbol id="i-check" viewBox="0 0 24 24"><path d="M5 12.5l4.5 4.5L19 7.5"/></symbol>
 <symbol id="i-lock" viewBox="0 0 24 24"><rect x="5" y="11" width="14" height="9" rx="2"/><path d="M8 11V8a4 4 0 0 1 8 0v3"/></symbol>
 <symbol id="i-x" viewBox="0 0 24 24"><path d="M6 6l12 12M18 6L6 18"/></symbol>
 <symbol id="i-left" viewBox="0 0 24 24"><path d="M15 6l-6 6 6 6"/></symbol>
 <symbol id="i-right" viewBox="0 0 24 24"><path d="M9 6l6 6-6 6"/></symbol>
 <symbol id="i-doc" viewBox="0 0 24 24"><path d="M7 3h7l5 5v13H7z"/><path d="M14 3v5h5"/></symbol>
 <symbol id="i-img" viewBox="0 0 24 24"><rect x="3" y="5" width="18" height="14" rx="2"/><circle cx="9" cy="10" r="1.6"/><path d="M21 16l-5-5-8 8"/></symbol>
</svg>
<header class="top"><div class="wrap">
 <svg class="ic logo" aria-hidden="true"><use href="#i-logo"/></svg>
 <div class="grow">
  <h1>DupeFinder report <span class="pill" id="ro-pill" hidden>Read-only</span></h1>
  <div class="hsub" id="hsub"></div>
 </div>
 <button class="btn" id="quit" type="button" hidden>Quit</button>
</div></header>
<main id="main"><div class="wrap">
 <section aria-label="Summary">
  <div class="stats" id="stats"></div>
  <div class="below">
   <details class="skip" id="skip"><summary id="skip-sum"></summary><div class="skipbox" id="skipbox"></div></details>
   <div class="tune" id="tune"></div>
  </div>
 </section>
 <section class="filters" aria-label="Filters">
  <div class="frow">
   <span class="flabel" id="lk">Show</span>
   <div class="seg" role="group" aria-labelledby="lk" id="f-kind">
    <button type="button" data-k="all" aria-pressed="true">All</button><button type="button" data-k="pdf" aria-pressed="false">PDFs</button><button type="button" data-k="image" aria-pressed="false">Photos</button>
   </div>
   <div class="chips" role="group" aria-label="Match type" id="f-types"></div>
  </div>
  <div class="frow">
   <label class="fld"><span class="flabel">Min score</span><input id="f-min" type="number" min="0" max="100" step="0.5" inputmode="decimal" placeholder="Any"><span class="flabel">%</span></label>
   <label class="fld ff"><span class="flabel">Folder</span><input id="f-folder" type="search" placeholder="Folder contains…" autocomplete="off" spellcheck="false"></label>
   <label class="fld"><span class="flabel">Sort</span><select id="f-sort"><option value="space">Space to free ↓</option><option value="id">Group number</option><option value="count">Most files</option></select></label>
   <span class="showing" id="showing" aria-live="polite"></span>
   <button class="btn sm quiet" type="button" id="f-reset" hidden>Reset filters</button>
  </div>
  <div class="frow bulk" id="bulk" hidden>
   <span class="flabel">Choices</span>
   <button class="btn sm" type="button" data-bulk="exact">Accept suggestions for exact duplicates</button>
   <button class="btn sm" type="button" data-bulk="all">Accept all suggestions</button>
   <button class="btn sm" type="button" data-bulk="clear">Clear all choices</button>
  </div>
  <div class="frow ro" id="ro-note" hidden>Read-only report: suggestions are shown but can't be changed here. Run DupeFinder without --report-only to move files.</div>
 </section>
 <div id="groups"></div>
 <div class="empty" id="empty" hidden><p id="empty-t">No groups match these filters.</p><button class="btn" type="button" id="reset">Reset filters</button></div>
 <div class="sentinel" id="sentinel" hidden><button class="btn" type="button" id="moregroups">Show more groups</button></div>
</div></main>
<div class="bar" id="bar" hidden><div class="wrap">
 <span class="barinfo" id="barinfo" aria-live="polite"></span>
 <button class="btn pri" id="move" type="button" disabled>Move 0 files (0 B) to Recycle Bin</button>
</div></div>
<div class="toasts" id="toasts"></div>
<div class="vh" id="live" aria-live="assertive"></div>
<dialog id="ask" aria-labelledby="ask-t">
 <form method="dialog" class="ask">
  <h2 id="ask-t"></h2>
  <div id="ask-b"></div>
  <div class="askb"><button class="btn" value="cancel" autofocus>Cancel</button><button class="btn pri" value="ok" id="ask-ok">OK</button></div>
 </form>
</dialog>
<dialog id="lb" aria-labelledby="lb-n">
 <div class="lb">
  <div class="lbh">
   <div class="grow"><div class="lbn" id="lb-n"></div><div class="lbs" id="lb-s"></div></div>
   <div class="btns">
    <span class="lbc" id="lb-c"></span>
    <button class="btn sm" type="button" id="lb-prev" aria-label="Previous photo"><svg class="ic" aria-hidden="true"><use href="#i-left"/></svg></button>
    <button class="btn sm" type="button" id="lb-next" aria-label="Next photo"><svg class="ic" aria-hidden="true"><use href="#i-right"/></svg></button>
    <button class="btn sm" type="button" id="lb-open">Open</button>
    <button class="btn sm" type="button" id="lb-x"><svg class="ic" aria-hidden="true"><use href="#i-x"/></svg>Close</button>
   </div>
  </div>
  <div class="lbb" id="lb-b">
   <div class="lbm"><img id="lb-img" alt=""><p class="lbnote" id="lb-note" hidden></p></div>
   <div class="crops" id="lb-crops" hidden>
    <figure><figcaption>Sharpest area (100%)</figcaption><div class="crop"><img id="lb-sharp" alt="Sharpest area at 100%"></div></figure>
    <figure><figcaption>Blurriest area (100%)</figcaption><div class="crop"><img id="lb-blur" alt="Blurriest area at 100%"></div></figure>
   </div>
  </div>
 </div>
</dialog>
<div class="stopped" id="stopped" hidden><div><svg class="ic logo" aria-hidden="true"><use href="#i-logo"/></svg><h2>DupeFinder has stopped. You can close this tab.</h2><p class="muted">Files you moved stay in the Recycle Bin or the _duplicates_review folders.</p></div></div>
<noscript><p style="padding:16px">This report needs JavaScript.</p></noscript>
<script>
window.DF_DATA = __DF_DATA__;
(function () {
'use strict';
const D = window.DF_DATA || {};
const SERVER = D.mode === 'server';
const TOKEN = String(D.token || '');
const SUM = D.summary || {};
const SKIP = D.skipped || {};
const CHUNK = 30, ROWS = 50, REQ_MAX = 200;
const $ = (id) => document.getElementById(id);

/* ---------- formatting ---------- */
const nf0 = new Intl.NumberFormat(undefined, { maximumFractionDigits: 0 });
const nf1 = new Intl.NumberFormat(undefined, { minimumFractionDigits: 1, maximumFractionDigits: 1 });
const nfa = new Intl.NumberFormat(undefined, { maximumFractionDigits: 1 });
const fmt = (n) => nf0.format(Number(n) || 0);
const num = (n) => (n == null || n === '' || isNaN(n) ? '–' : nfa.format(Number(n)));
const plural = (n, one, many) => fmt(n) + ' ' + (Number(n) === 1 ? one : (many || one + 's'));
function human(b) {
  b = Number(b) || 0;
  if (b < 1024) return fmt(b) + ' B';
  const u = ['KB', 'MB', 'GB', 'TB', 'PB'];
  let i = -1;
  do { b /= 1024; i++; } while (b >= 1023.95 && i < u.length - 1);
  return (b >= 99.95 ? nf0 : nf1).format(b) + ' ' + u[i];
}
function dur(s) {
  s = Math.max(0, Math.round(Number(s) || 0));
  if (s < 60) return s + ' s';
  if (s < 3600) return Math.floor(s / 60) + ' min ' + (s % 60) + ' s';
  return Math.floor(s / 3600) + ' h ' + Math.floor((s % 3600) / 60) + ' min';
}

/* ---------- DOM helpers (never innerHTML: names and paths are untrusted) ---------- */
function el(tag, a, ...kids) {
  const e = document.createElement(tag);
  if (a) for (const k of Object.keys(a)) {
    const v = a[k];
    if (v == null || v === false) continue;
    if (k === 'text') e.textContent = String(v);
    else if (k === 'class') e.className = v;
    else if (typeof v === 'function') e.addEventListener(k.slice(2), v);
    else if (k.startsWith('on')) continue;
    else if (v === true) e[k] = true;
    else e.setAttribute(k, String(v));
  }
  for (const c of kids) if (c != null && c !== false && c !== '') e.append(c);
  return e;
}
const SVGNS = 'http://www.w3.org/2000/svg';
function icon(id) {
  const s = document.createElementNS(SVGNS, 'svg');
  s.setAttribute('class', 'ic'); s.setAttribute('aria-hidden', 'true');
  const u = document.createElementNS(SVGNS, 'use'); u.setAttribute('href', '#i-' + id);
  s.append(u); return s;
}
function midTrunc(text, cls, tail, title) {
  text = String(text == null ? '' : text);
  const cut = Math.max(0, text.length - tail);
  return el('div', { class: 'mt ' + cls, title: title || text }, el('span', { class: 'h', text: text.slice(0, cut) }), el('span', { class: 't', text: text.slice(cut) }));
}
function urlOk(u, img) {
  if (typeof u !== 'string' || !u) return null;
  const m = /^([a-z][a-z0-9+.-]*):/i.exec(u);
  if (!m) return u.startsWith('//') ? null : u;
  const s = m[1].toLowerCase();
  if (s === 'file' || s === 'blob' || (img && s === 'data' && /^data:image\//i.test(u))) return u;
  return null;
}
const srv = (what, fid, extra) => '/' + what + '/' + encodeURIComponent(String(fid)) + (extra ? '/' + extra : '') + '?token=' + encodeURIComponent(TOKEN);

/* ---------- labels ---------- */
const TYPES = [['identical', 'Identical'], ['same', 'Same content'], ['similar', 'Similar'], ['burst', 'Burst'], ['related', 'Related'], ['poor', 'Possibly poor']];
const TYPE_LABEL = Object.fromEntries(TYPES);
const LK_TIP = {
  keeper: 'The copy suggested to keep', identical: 'Byte-for-byte the same file', same_text: 'Same words, different file',
  same_pixels: 'Same image, different metadata', similar_text: 'Share of 5-word chunks the two files have in common',
  visual: 'How alike the two photos look', visual_pdf: 'How alike the pages look, adjusted for page count',
  burst: 'Taken seconds apart on the same camera', ai: 'Same scene or subject, judged by the AI model',
  related: 'A photo matches a page inside a PDF; never pre-selected', via: 'Joined the group through another file; never pre-selected',
  poor: 'Flagged by the quality check; nothing here is pre-selected',
};
const FLAGS = {
  blurry: ['Blurry', 'Sharpness is below the blur cutoff'], dark: ['Dark', 'Median brightness is below the dark cutoff'],
  clipped: ['Clipped', 'Many pixels are pure black or pure white'], system: ['System folder', 'In a system folder; never pre-selected'],
  hardlinked: ['Hard-linked', 'Other names point to the same data; DupeFinder won’t move it'],
  protected: ['Password-protected', 'Only the file fingerprint could be checked'], error: ['Read error', 'Part of the check failed for this file'],
  prefer: ['Preferred folder', 'Under a --prefer folder'],
};

/* ---------- state ---------- */
const key = (f) => String(f);
const GS = (Array.isArray(D.groups) ? D.groups : []).map((g, i) => {
  const ms = Array.isArray(g.members) ? g.members : [];
  let pdf = 0, img = 0;
  for (const m of ms) { if (m.kind === 'pdf') pdf++; else img++; }
  const n = Number(g.id);
  return { g, i, ms, pdf, img, num: isFinite(n) ? n : i + 1, fold: ms.map((m) => String(m.folder || '').toLowerCase()).join('\n'), card: null, shown: 0, inView: true };
});
const byFid = new Map(), choice = new Map(), moved = new Map(), refused = new Map(), rows = new Map();
const initial = (m) => (!m.locked && m.suggest === 'trash' ? 'trash' : 'keep');
for (const G of GS) for (const m of G.ms) { const k = key(m.fid); byFid.set(k, { m, G }); choice.set(k, initial(m)); }
const F = { kind: 'all', types: new Set(), min: null, folder: '', sort: 'space' };
let view = [], shown = 0, busy = false, gone = false;

/* ---------- header and summary ---------- */
function renderHeader() {
  const roots = Array.isArray(SUM.roots) ? SUM.roots.join(', ') : '';
  $('hsub').textContent = [D.generated ? 'Generated ' + D.generated : '', roots ? 'Scanned ' + roots : ''].filter(Boolean).join(' · ');
  $('hsub').title = $('hsub').textContent;
  $('quit').hidden = !SERVER; $('bar').hidden = !SERVER; $('bulk').hidden = !SERVER;
  $('ro-pill').hidden = SERVER; $('ro-note').hidden = SERVER;
  const tiles = [
    ['Files scanned', fmt(SUM.files_scanned), plural(SUM.pdfs, 'PDF') + ' · ' + plural(SUM.photos, 'photo')],
    ['Duplicate groups', fmt(SUM.groups)],
    ['Bursts', fmt(SUM.bursts)],
    ['Possibly poor shots', fmt(SUM.poor)],
    ['Space you could free', human(SUM.space_to_free), 'with the suggestions', 'hi'],
    ['Scan time', dur(SUM.scan_seconds)],
  ];
  $('stats').append(...tiles.map(([k, v, s, c]) => el('div', { class: 'stat' + (c ? ' ' + c : '') }, el('div', { class: 'k', text: k }), el('div', { class: 'v', text: v }), s ? el('div', { class: 's', text: s }) : null)));
  // skipped
  const counts = SKIP.counts && typeof SKIP.counts === 'object' ? SKIP.counts : {};
  const total = Object.values(counts).reduce((a, n) => a + (Number(n) || 0), 0);
  const fl = Array.isArray(SKIP.folders) ? SKIP.folders : [], er = Array.isArray(SKIP.errors) ? SKIP.errors : [], pr = Array.isArray(SKIP.protected) ? SKIP.protected : [];
  const det = $('skip');
  if (!total && !fl.length && !er.length && !pr.length) det.hidden = true;
  $('skip-sum').textContent = 'Skipped: ' + plural(total, 'item') + (er.length ? ' · ' + plural(er.length, 'error') : '');
  det.addEventListener('toggle', () => {
    const box = $('skipbox');
    if (!det.open || box.childElementCount) return;
    const keys = Object.keys(counts);
    if (keys.length) box.append(el('div', null, el('h3', { text: 'What was skipped' }), el('div', { class: 'kv' }, ...keys.flatMap((k) => [el('span', { text: k }), el('b', { text: fmt(counts[k]) })]))));
    const list = (title, items, fn) => {
      if (!items.length) return;
      const ul = el('ul', { class: 'plist' });
      for (const it of items.slice(0, 300)) { const [p, why] = fn(it); ul.append(el('li', null, el('span', { class: 'p', title: p, text: p }), el('span', { class: 'why', text: why }))); }
      if (items.length > 300) ul.append(el('li', { class: 'muted', text: '…and ' + fmt(items.length - 300) + ' more. The full list is in the log.' }));
      box.append(el('div', null, el('h3', { text: title + ' (' + fmt(items.length) + ')' }), ul));
    };
    list('Folders not scanned', fl, (f) => [String(f.path || ''), String(f.reason || '')]);
    list('Files that couldn’t be read', er, (f) => [String(f.path || ''), String(f.error || '')]);
    list('Password-protected PDFs', pr, (p) => [String(p), '']);
  });
  // cutoffs and sharpness percentiles
  const st = SUM.settings || {};
  let clip = Number(st.clip_cutoff); if (clip > 0 && clip <= 1) clip *= 100;
  const parts = ['Cutoffs: score ≥ ' + num(st.min_score) + '%', 'visual ≤ ' + num(st.max_bits) + ' bits', 'burst gap ' + num(st.burst_gap) + ' s',
    'blur < ' + (st.blur_cutoff == null ? 'not set' : num(st.blur_cutoff)), 'dark < ' + num(st.dark_cutoff), 'clipped > ' + num(clip) + '%'];
  const sp = SUM.sharpness_percentiles;
  const tune = $('tune');
  tune.append(el('div', { text: parts.join(' · ') }));
  if (sp) tune.append(el('div', { text: 'Photo sharpness in this scan: 10th percentile ' + num(sp.p10) + ' · median ' + num(sp.p50) + ' · 90th percentile ' + num(sp.p90) + '. Photos below --blur-cutoff are flagged as blurry.' }));
}

/* ---------- filters ---------- */
function setupFilters() {
  const tcount = {};
  for (const G of GS) tcount[G.g.type] = (tcount[G.g.type] || 0) + 1;
  const chips = $('f-types');
  for (const [t, label] of TYPES) {
    const b = el('button', { type: 'button', class: 'chip', 'data-t': t, 'aria-pressed': 'false' }, label, el('span', { text: fmt(tcount[t] || 0) }));
    b.addEventListener('click', () => { if (F.types.has(t)) F.types.delete(t); else F.types.add(t); b.setAttribute('aria-pressed', String(F.types.has(t))); applyFilters(); });
    chips.append(b);
  }
  $('f-kind').addEventListener('click', (e) => {
    const b = e.target.closest('button'); if (!b) return;
    F.kind = b.dataset.k;
    for (const x of $('f-kind').children) x.setAttribute('aria-pressed', String(x === b));
    applyFilters();
  });
  $('f-min').addEventListener('input', (e) => { const v = e.target.value.trim(); F.min = v === '' || isNaN(v) ? null : Number(v); applyFilters(); });
  let t = 0;
  $('f-folder').addEventListener('input', (e) => { clearTimeout(t); t = setTimeout(() => { F.folder = e.target.value.trim().toLowerCase(); applyFilters(); }, 150); });
  $('f-sort').addEventListener('change', (e) => { F.sort = e.target.value; applyFilters(); });
  const reset = () => {
    F.kind = 'all'; F.types.clear(); F.min = null; F.folder = ''; F.sort = 'space';
    for (const x of $('f-kind').children) x.setAttribute('aria-pressed', String(x.dataset.k === 'all'));
    for (const x of chips.children) x.setAttribute('aria-pressed', 'false');
    $('f-min').value = ''; $('f-folder').value = ''; $('f-sort').value = 'space';
    applyFilters();
  };
  $('reset').addEventListener('click', reset);
  $('f-reset').addEventListener('click', reset);
  $('bulk').addEventListener('click', (e) => { const b = e.target.closest('[data-bulk]'); if (b) bulk(b.dataset.bulk); });
}
function matches(G) {
  if (F.kind === 'pdf' && !G.pdf) return false;
  if (F.kind === 'image' && !G.img) return false;
  if (F.types.size && !F.types.has(G.g.type)) return false;
  if (F.min != null && G.g.min_score != null && Number(G.g.min_score) < F.min) return false;
  if (F.folder && !G.fold.includes(F.folder)) return false;
  return true;
}
const byNum = (a, b) => a.num - b.num || a.i - b.i;
function applyFilters() {
  view = GS.filter(matches);
  if (F.sort === 'id') view.sort(byNum);
  else if (F.sort === 'count') view.sort((a, b) => b.ms.length - a.ms.length || byNum(a, b));
  else view.sort((a, b) => (Number(b.g.space) || 0) - (Number(a.g.space) || 0) || byNum(a, b));
  for (const G of GS) G.inView = false;
  for (const G of view) G.inView = true;
  $('groups').textContent = '';
  shown = 0;
  $('showing').textContent = 'Showing ' + fmt(view.length) + ' of ' + plural(GS.length, 'group');
  $('empty').hidden = view.length > 0;
  $('f-reset').hidden = !(F.kind !== 'all' || F.types.size || F.min != null || F.folder);
  $('empty-t').textContent = GS.length ? 'No groups match these filters.' : 'No duplicates found.';
  $('reset').hidden = !GS.length;
  renderMore();
  updateBar();
}

/* ---------- lazy group rendering ---------- */
const io = 'IntersectionObserver' in window ? new IntersectionObserver((es) => { if (es.some((e) => e.isIntersecting)) renderMore(); }, { rootMargin: '0px 0px 900px 0px' }) : null;
function renderMore() {
  const end = Math.min(view.length, shown + CHUNK);
  if (end > shown) {
    const frag = document.createDocumentFragment();
    for (let i = shown; i < end; i++) frag.append(view[i].card ? view[i].card.root : buildCard(view[i]));
    $('groups').append(frag);
    shown = end;
  }
  const s = $('sentinel');
  s.hidden = shown >= view.length;
  $('moregroups').textContent = 'Show more groups (' + fmt(view.length - shown) + ' left)';
  if (io && !s.hidden) { io.unobserve(s); io.observe(s); }
}

/* ---------- group cards ---------- */
function buildCard(G) {
  const g = G.g;
  const hid = 'g-' + G.i;
  const space = el('b'), freed = el('span', { class: 'gfreed', hidden: true });
  const kinds = [G.pdf ? plural(G.pdf, 'PDF') : '', G.img ? plural(G.img, 'photo') : ''].filter(Boolean).join(' · ');
  const extra = [g.type === 'burst' && g.burst_shots ? plural(g.burst_shots, 'shot') : kinds];
  if (g.min_score != null) extra.push('lowest score ' + num(g.min_score) + '%');
  const root = el('section', { class: 'card', 'data-gid': String(g.id), 'data-type': g.type, 'aria-labelledby': hid },
    el('div', { class: 'ghead' },
      el('div', { class: 'gtop' },
        el('h2', { class: 'gnum', id: hid, text: 'Group ' + (g.id != null ? g.id : G.i + 1) }),
        el('span', { class: 'badge tb tb-' + g.type, text: TYPE_LABEL[g.type] || String(g.type || '') }),
        el('span', { class: 'gk', text: extra.join(' · ') }),
        el('span', { class: 'gspace' }, 'Space to free ', space), freed),
      g.title ? el('div', { class: 'gtitle', title: String(g.title), text: String(g.title) }) : null));
  const list = el('div', { class: 'rows', role: 'list' });
  const more = el('button', { class: 'btn sm more', type: 'button', hidden: true });
  more.addEventListener('click', () => renderRows(G));
  root.append(list, more);
  G.card = { root, space, freed, list, more };
  renderRows(G);
  refreshCard(G);
  return root;
}
function renderRows(G) {
  const start = G.shown, end = Math.min(G.ms.length, start + ROWS);
  const frag = document.createDocumentFragment();
  for (let i = start; i < end; i++) frag.append(buildRow(G.ms[i], G));
  G.card.list.append(frag);
  G.shown = end;
  const left = G.ms.length - end;
  G.card.more.hidden = left <= 0;
  G.card.more.textContent = left > ROWS ? 'Show ' + ROWS + ' more (' + fmt(left) + ' not shown)' : 'Show ' + fmt(left) + ' more';
}
function refreshCard(G) {
  if (!G.card) return;
  let sp = 0, fr = 0;
  for (const m of G.ms) {
    const k = key(m.fid);
    if (moved.has(k)) fr += Number(m.size) || 0;
    else if (choice.get(k) === 'trash') sp += Number(m.size) || 0;
  }
  G.card.space.textContent = human(sp);
  G.card.freed.hidden = !fr;
  G.card.freed.textContent = fr ? 'Freed ' + human(fr) : '';
}

/* ---------- file rows ---------- */
function buildThumb(m, G) {
  const isImg = m.kind === 'image';
  const ext = String(m.ext || (String(m.name).split('.').pop() || '')).toUpperCase().slice(0, 5);
  const ph = () => el('span', { class: 'ph' }, icon(isImg ? 'img' : 'doc'), ext);
  const src = SERVER ? srv('thumb', m.fid) : urlOk(m.thumb, true);
  let t;
  if (isImg) {
    t = el('button', { type: 'button', class: 'thumb', 'aria-label': 'View ' + m.name, title: 'View full size' });
    t.addEventListener('click', () => openLightbox(m, G));
  } else {
    const href = SERVER ? srv('view', m.fid) : urlOk(m.file_url);
    t = href ? el('a', { class: 'thumb', href, target: '_blank', rel: 'noopener', 'aria-label': 'Open PDF ' + m.name, title: 'Open the PDF in a new tab' }) : el('span', { class: 'thumb' });
  }
  if (src) {
    const img = el('img', { alt: '', loading: 'lazy', decoding: 'async', width: '64', height: '64' });
    img.addEventListener('error', () => img.replaceWith(ph()), { once: true });
    img.src = src;
    t.append(img);
  } else t.append(ph());
  return t;
}
function buildRow(m, G) {
  const k = key(m.fid);
  const name = String(m.name == null ? '' : m.name), path = String(m.path || name);
  const dot = name.lastIndexOf('.');
  const folder = String(m.folder == null ? '' : m.folder);
  const slash = folder.lastIndexOf(folder.includes('\\') ? '\\' : '/', folder.length - 2);
  const info = el('div', { class: 'info' },
    midTrunc(name, 'name', dot > 0 ? name.length - dot + 6 : 8, path),
    midTrunc(folder, 'folder', slash >= 0 ? folder.length - slash : 0, folder));
  const line = el('div', { class: 'line' });
  const lk = m.keeper ? 'keeper' : String(m.label_kind || 'none');
  if (m.keeper) line.append(el('span', { class: 'badge lk lk-keeper', title: LK_TIP.keeper }, icon('check'), m.label ? String(m.label) : 'Kept copy'));
  else if (m.label) line.append(el('span', { class: 'badge lk lk-' + lk, title: LK_TIP[lk] || '', text: m.label }));
  for (const f of Array.isArray(m.flags) ? m.flags : []) {
    const d = FLAGS[f];
    line.append(el('span', { class: 'badge flag fl-' + f, title: d ? d[1] : '', text: d ? d[0] : f }));
  }
  const meta = [human(m.size), m.dims ? String(m.dims) : m.pages != null ? plural(m.pages, 'page') : '',
    m.date ? (m.date_kind === 'taken' ? 'Taken ' : 'Modified ') + m.date : ''].filter(Boolean);
  line.append(el('span', { class: 'metaw' }, el('span', { class: 'metas' }, ...meta.map((t) => el('span', { class: 'meta', text: t })))));
  info.append(line);
  if (m.detail) info.append(el('div', { class: 'detail', text: m.detail }));
  const t = G.g.type;
  if (m.rel_sharpness != null || ((t === 'burst' || t === 'poor') && (m.sharpness != null || m.exposure))) {
    const q = el('div', { class: 'qual' });
    if (m.rel_sharpness != null) {
      const r = Math.max(0, Math.min(100, Number(m.rel_sharpness) || 0));
      const bar = el('span', { class: 'sbar' + (r < 60 ? ' lo' : ''), role: 'meter', 'aria-valuemin': '0', 'aria-valuemax': '100', 'aria-valuenow': String(r), 'aria-label': 'Sharpness relative to the sharpest shot' }, el('i'));
      bar.firstChild.style.width = r + '%';
      q.append(el('span', null, bar, 'Sharpness ' + fmt(r) + (r === 100 ? ' (sharpest)' : '')));
    } else if (m.sharpness != null) q.append(el('span', { text: 'Sharpness ' + num(m.sharpness) }));
    if (m.exposure) q.append(el('span', { class: m.exposure === 'OK' ? '' : 'expw', text: 'Exposure: ' + m.exposure }));
    info.append(q);
  }
  if (m.locked) info.append(el('div', { class: 'lock' }, icon('lock'), el('span', { text: m.lock_reason || 'DupeFinder won’t move this file.' })));
  const note = el('div', { class: 'note', hidden: true });
  info.append(note);
  const act = el('div', { class: 'actions' });
  const r = { row: null, note, keep: null, trash: null, open: null, reveal: null, timer: 0 };
  if (SERVER) {
    r.keep = el('button', { type: 'button', 'data-choice': 'keep', text: 'Keep' });
    r.trash = el('button', { type: 'button', 'data-choice': 'trash', text: 'Trash' });
    r.keep.addEventListener('click', () => choose(k, 'keep'));
    r.trash.addEventListener('click', () => choose(k, 'trash'));
    if (m.locked) r.trash.title = m.lock_reason || 'DupeFinder won’t move this file';
    r.open = el('button', { type: 'button', class: 'btn sm', text: 'Open' });
    r.reveal = el('button', { type: 'button', class: 'btn sm', text: 'Show in folder' });
    r.open.addEventListener('click', () => openFile(m, false));
    r.reveal.addEventListener('click', () => openFile(m, true));
    act.append(el('div', { class: 'seg tg', role: 'group', 'aria-label': 'Keep or trash ' + name }, r.keep, r.trash), r.open, r.reveal);
  } else {
    const tr = initial(m) === 'trash';
    act.append(el('span', { class: 'badge sugg ' + (tr ? 'sugg-t' : 'sugg-k'), text: 'Suggested: ' + (tr ? 'Trash' : 'Keep') }), linkBtn('Open', m.file_url), linkBtn('Show in folder', m.folder_url));
  }
  r.row = el('div', { class: 'row', role: 'listitem', 'data-fid': k }, buildThumb(m, G), info, act);
  rows.set(k, r);
  paintRow(k);
  return r.row;
}
function linkBtn(text, url) {
  const u = urlOk(url);
  return u ? el('a', { class: 'btn sm', href: u, target: '_blank', rel: 'noopener', text }) : el('span', { class: 'btn sm off', 'aria-disabled': 'true', title: 'Not available', text });
}
function setNote(r, text, cls) {
  r.note.textContent = text; r.note.className = 'note' + (cls ? ' ' + cls : ''); r.note.hidden = !text;
}
function paintRow(k) {
  const r = rows.get(k); if (!r) return;
  const { m } = byFid.get(k);
  const c = choice.get(k), mv = moved.get(k), rf = refused.get(k);
  r.row.classList.toggle('trash', c === 'trash' && !mv);
  r.row.classList.toggle('moved', !!mv);
  if (r.keep) {
    r.keep.setAttribute('aria-pressed', String(c === 'keep'));
    r.trash.setAttribute('aria-pressed', String(c === 'trash'));
    r.keep.disabled = !!mv; r.trash.disabled = !!mv || !!m.locked;
    r.open.disabled = r.reveal.disabled = !!mv;
  }
  if (r.timer) return;
  if (mv) setNote(r, mv.action === 'review' ? 'Moved to ' + (mv.dest || 'a _duplicates_review folder') : 'Moved to Recycle Bin', 'ok');
  else if (rf) setNote(r, 'Not moved: ' + rf, 'err');
  else setNote(r, '', '');
}
function warnRow(k, text) {
  const r = rows.get(k); if (!r) return;
  clearTimeout(r.timer);
  setNote(r, text, 'warn');
  $('live').textContent = text;
  r.timer = setTimeout(() => { r.timer = 0; paintRow(k); }, 5000);
}

/* ---------- choices ---------- */
const keptOthers = (G, m) => G.ms.some((o) => o !== m && !moved.has(key(o.fid)) && choice.get(key(o.fid)) === 'keep');
function choose(k, c) {
  const it = byFid.get(k);
  if (!it || !SERVER || busy || moved.has(k) || choice.get(k) === c) return;
  const { m, G } = it;
  if (c === 'trash') {
    if (m.locked) return;
    if (G.g.type !== 'poor' && !keptOthers(G, m)) { warnRow(k, 'Keep at least one copy in each group'); return; }
  }
  const r = rows.get(k);
  if (r && r.timer) { clearTimeout(r.timer); r.timer = 0; }
  choice.set(k, c);
  paintRow(k); refreshCard(G); updateBar();
}
function fixKeepOne(G) {
  if (G.g.type === 'poor') return;
  const left = G.ms.filter((m) => !moved.has(key(m.fid)));
  if (!left.length || left.some((m) => choice.get(key(m.fid)) === 'keep')) return;
  choice.set(key((left.find((m) => m.keeper) || left[0]).fid), 'keep');
}
function bulk(mode) {
  if (!SERVER || busy) return;
  let groups = 0;
  for (const G of GS) {
    if (mode === 'exact' && G.g.type !== 'identical' && G.g.type !== 'same') continue;
    groups++;
    for (const m of G.ms) { const k = key(m.fid); if (!moved.has(k)) choice.set(k, mode === 'clear' ? 'keep' : initial(m)); }
    if (mode !== 'clear') fixKeepOne(G);
  }
  for (const k of rows.keys()) paintRow(k);
  for (const G of GS) refreshCard(G);
  const t = updateBar();
  const now = ' ' + plural(t.n, 'file') + ' (' + human(t.b) + ') set to Trash.';
  toast(mode === 'clear' ? 'Choices cleared: every file is set to Keep.' : mode === 'exact' ? 'Suggestions applied to ' + plural(groups, 'exact-duplicate group') + '.' + now : 'All suggestions applied.' + now);
}
function totals() {
  let n = 0, b = 0, hid = 0, poor = 0, freed = 0, mv = 0;
  for (const G of GS) for (const m of G.ms) {
    const k = key(m.fid);
    if (moved.has(k)) { mv++; freed += Number(m.size) || 0; continue; }
    if (choice.get(k) !== 'trash') continue;
    n++; b += Number(m.size) || 0;
    if (!G.inView) hid++;
    if (G.g.type === 'poor') poor++;
  }
  return { n, b, hid, poor, freed, mv };
}
function updateBar() {
  const t = totals();
  if (!SERVER) return t;
  const btn = $('move');
  btn.disabled = !t.n || busy || gone;
  if (!busy) btn.textContent = 'Move ' + plural(t.n, 'file') + ' (' + human(t.b) + ') to Recycle Bin';
  const info = [];
  if (t.hid) info.push(fmt(t.hid) + ' of them ' + (t.hid === 1 ? 'is' : 'are') + ' in groups hidden by the filters');
  if (t.mv) info.push(plural(t.mv, 'file') + ' moved so far · ' + human(t.freed) + ' freed');
  if (gone) info.push('DupeFinder isn’t running');
  $('barinfo').textContent = info.join(' · ');
  return t;
}

/* ---------- server calls ---------- */
function serverGone() {
  if (!gone) toast('DupeFinder isn’t running any more — actions are unavailable.', 'err', 0);
  gone = true;
  updateBar();
}
async function post(path, body) {
  let res;
  try {
    res = await fetch(path, { method: 'POST', cache: 'no-store', headers: { 'Content-Type': 'application/json', 'X-Token': TOKEN }, body: JSON.stringify(body) });
  } catch (e) { serverGone(); return null; }
  if (!res.ok) {
    let why = '';
    try { const j = await res.json(); why = j && (j.reason || j.error) ? String(j.reason || j.error) : ''; } catch (e) { /* not JSON */ }
    toast('DupeFinder refused the request' + (why ? ': ' + why : ' (HTTP ' + res.status + ').'), 'err');
    return null;
  }
  try { return await res.json(); } catch (e) { toast('DupeFinder sent an answer this page can’t read.', 'err'); return null; }
}
async function openFile(m, reveal) {
  if (!SERVER) { const u = urlOk(reveal ? m.folder_url : m.file_url); if (u) window.open(u, '_blank', 'noopener'); return; }
  if (gone) { toast('DupeFinder isn’t running any more — actions are unavailable.', 'err'); return; }
  const r = await post('/api/open', { fid: m.fid, reveal: !!reveal });
  if (r && !r.ok) toast((reveal ? 'Couldn’t show ' : 'Couldn’t open ') + m.name + (r.reason ? ': ' + r.reason : '.'), 'err');
}
async function startMove() {
  if (busy || gone) return;
  const batches = [];
  let n = 0, b = 0, poor = 0;
  for (const G of GS) {
    const ids = [];
    for (const m of G.ms) {
      const k = key(m.fid);
      if (choice.get(k) === 'trash' && !moved.has(k)) { ids.push(m.fid); b += Number(m.size) || 0; if (G.g.type === 'poor') poor++; }
    }
    if (ids.length) { batches.push(ids); n += ids.length; }
  }
  if (!n) return;
  const lines = [
    { text: plural(n, 'file') + ' (' + human(b) + ') from ' + plural(batches.length, 'group') + ' will be moved to the Recycle Bin.' },
    { text: 'Files on USB or network drives go to a _duplicates_review folder on that drive instead of the Recycle Bin.', cls: 'muted' },
  ];
  if (poor) lines.push({ text: fmt(poor) + ' of these ' + (poor === 1 ? 'has' : 'have') + ' no other copy.', cls: 'warn' });
  if (!(await ask('Move ' + plural(n, 'file') + ' (' + human(b) + ')?', lines, 'Move to Recycle Bin'))) return;
  const reqs = [];
  let cur = [];
  for (const ids of batches) { if (cur.length && cur.length + ids.length > REQ_MAX) { reqs.push(cur); cur = []; } cur = cur.concat(ids); }
  if (cur.length) reqs.push(cur);
  busy = true;
  const btn = $('move');
  btn.disabled = true; btn.classList.add('busy'); btn.setAttribute('aria-busy', 'true');
  let done = 0, rec = 0, rev = 0, bad = 0;
  const touched = new Set();
  for (const ids of reqs) {
    btn.textContent = 'Moving… ' + fmt(done) + ' of ' + fmt(n) + ' files';
    const res = await post('/api/move', { ids });
    if (!res) break;
    const seen = new Set();
    for (const r of Array.isArray(res.results) ? res.results : []) {
      const k = key(r && r.fid), it = byFid.get(k);
      if (!it || seen.has(k)) continue;
      seen.add(k); touched.add(it.G);
      if (r.ok) { moved.set(k, { action: r.action, dest: r.dest }); refused.delete(k); if (r.action === 'review') rev++; else rec++; }
      else { refused.set(k, String(r.reason || 'refused by DupeFinder')); bad++; }
      paintRow(k);
    }
    for (const f of ids) {
      const k = key(f);
      if (!seen.has(k)) { refused.set(k, 'no answer from DupeFinder for this file'); bad++; touched.add(byFid.get(k).G); paintRow(k); }
    }
    done += ids.length;
  }
  busy = false;
  btn.classList.remove('busy'); btn.removeAttribute('aria-busy');
  touched.forEach(refreshCard);
  updateBar();
  const parts = [];
  if (rec) parts.push('Moved ' + plural(rec, 'file') + ' to the Recycle Bin');
  if (rev) parts.push((rec ? '' : 'Moved ') + plural(rev, 'file') + ' to a _duplicates_review folder');
  if (bad) parts.push(plural(bad, 'file') + ' not moved (see the red notes)');
  if (parts.length) toast(parts.join(' · ') + '.', bad ? 'warn' : 'ok', bad ? 12000 : 7000);
}
async function quit() {
  if (!(await ask('Quit DupeFinder?', [{ text: 'The report stops working and choices you haven’t applied are lost. Files you already moved stay where they are.' }], 'Quit'))) return;
  try { await fetch('/api/quit', { method: 'POST', cache: 'no-store', headers: { 'Content-Type': 'application/json', 'X-Token': TOKEN }, body: '{}' }); } catch (e) { /* already stopped */ }
  gone = true;
  $('main').inert = true; $('bar').hidden = true; $('quit').disabled = true;
  $('toasts').textContent = '';
  $('stopped').hidden = false;
  document.title = 'DupeFinder (stopped)';
}

/* ---------- toasts ---------- */
function toast(text, kind, ms) {
  kind = kind || 'ok';
  const x = el('button', { type: 'button', class: 'x', 'aria-label': 'Dismiss' }, icon('x'));
  const t = el('div', { class: 'toast ' + kind, role: kind === 'err' ? 'alert' : 'status' }, el('span', { text }), x);
  x.addEventListener('click', () => t.remove());
  const box = $('toasts');
  box.append(t);
  while (box.childElementCount > 4) box.firstElementChild.remove();
  if (ms !== 0) setTimeout(() => t.remove(), ms || 7000);
}

/* ---------- dialogs ---------- */
function trapFocus(d) {
  d.addEventListener('keydown', (e) => {
    if (e.key !== 'Tab') return;
    const f = [...d.querySelectorAll('button,[href],input,select,[tabindex]:not([tabindex="-1"])')].filter((x) => !x.disabled && x.getClientRects().length);
    if (!f.length) { e.preventDefault(); return; }
    const first = f[0], last = f[f.length - 1], a = document.activeElement;
    if (!d.contains(a)) { e.preventDefault(); first.focus(); }
    else if (e.shiftKey && a === first) { e.preventDefault(); last.focus(); }
    else if (!e.shiftKey && a === last) { e.preventDefault(); first.focus(); }
  });
  d.addEventListener('click', (e) => { if (e.target === d) d.close('cancel'); });
}
function ask(title, lines, okText) {
  const d = $('ask');
  $('ask-t').textContent = title;
  const b = $('ask-b'); b.textContent = '';
  for (const l of lines) b.append(el('p', { class: l.cls || '', text: l.text }));
  $('ask-ok').textContent = okText;
  d.returnValue = '';
  const back = document.activeElement;
  d.showModal();
  return new Promise((resolve) => {
    d.addEventListener('close', () => { resolve(d.returnValue === 'ok'); if (back && back.focus) back.focus(); }, { once: true });
  });
}
const lb = $('lb'), lbImg = $('lb-img'), lbNote = $('lb-note');
let lbList = [], lbIdx = 0, lbBack = null;
function openLightbox(m, G) {
  lbList = G.ms.filter((x) => x.kind === 'image');
  lbIdx = Math.max(0, lbList.indexOf(m));
  lbBack = document.activeElement;
  showLb();
  if (!lb.open) lb.showModal();
  $('lb-x').focus();
}
function lbFail() {
  const m = lbList[lbIdx];
  lbImg.hidden = true;
  lbNote.hidden = false;
  lbNote.textContent = /^(heic|heif|avif|tif|tiff|cr2|nef|arw|dng)$/i.test(String(m && m.ext))
    ? 'Your browser can’t display ' + String(m.ext).toUpperCase() + ' photos. Use Open to view it in your photo app.'
    : 'This photo couldn’t be shown here. Use Open to view it in your photo app.';
}
function showLb() {
  const m = lbList[lbIdx];
  if (!m) return;
  $('lb-n').textContent = m.name; $('lb-n').title = m.path || m.name;
  $('lb-s').textContent = [m.dims, human(m.size), m.date ? (m.date_kind === 'taken' ? 'Taken ' : 'Modified ') + m.date : '', m.label].filter(Boolean).join(' · ');
  $('lb-c').textContent = lbList.length > 1 ? fmt(lbIdx + 1) + ' of ' + fmt(lbList.length) : '';
  $('lb-prev').hidden = $('lb-next').hidden = lbList.length < 2;
  $('lb-prev').disabled = lbIdx === 0; $('lb-next').disabled = lbIdx === lbList.length - 1;
  $('lb-open').hidden = !(SERVER || urlOk(m.file_url));
  lbNote.hidden = true; lbImg.hidden = false; lbImg.alt = m.name;
  const src = SERVER ? srv('view', m.fid) : urlOk(m.file_url, true);
  if (src) lbImg.src = src; else { lbImg.removeAttribute('src'); lbFail(); }
  const crops = SERVER && !!m.crops;
  $('lb-crops').hidden = !crops;
  $('lb-b').classList.toggle('wc', crops);
  for (const [id, which] of [['lb-sharp', 'sharp'], ['lb-blur', 'blur']]) {
    const im = $(id);
    im.hidden = false;
    if (im.nextSibling) im.nextSibling.remove();
    if (crops) im.src = srv('crop', m.fid, which); else im.removeAttribute('src');
  }
}
function lbStep(d) { const i = lbIdx + d; if (i >= 0 && i < lbList.length) { lbIdx = i; showLb(); } }
function setupDialogs() {
  trapFocus($('ask')); trapFocus(lb);
  lbImg.addEventListener('error', () => { if (lbImg.getAttribute('src')) lbFail(); });
  for (const id of ['lb-sharp', 'lb-blur']) {
    const im = $(id);
    im.addEventListener('error', () => { if (im.getAttribute('src') && !im.hidden) { im.hidden = true; im.after('Not available'); } });
  }
  $('lb-x').addEventListener('click', () => lb.close());
  $('lb-prev').addEventListener('click', () => lbStep(-1));
  $('lb-next').addEventListener('click', () => lbStep(1));
  $('lb-open').addEventListener('click', () => { const m = lbList[lbIdx]; if (m) openFile(m, false); });
  lb.addEventListener('keydown', (e) => {
    if (e.key === 'ArrowLeft') { e.preventDefault(); lbStep(-1); }
    else if (e.key === 'ArrowRight') { e.preventDefault(); lbStep(1); }
  });
  lb.addEventListener('close', () => {
    lbImg.removeAttribute('src');
    for (const id of ['lb-sharp', 'lb-blur']) $(id).removeAttribute('src');
    if (lbBack && lbBack.isConnected && lbBack.focus) lbBack.focus();
  });
}

/* ---------- start ---------- */
renderHeader();
setupFilters();
setupDialogs();
$('move').addEventListener('click', startMove);
$('quit').addEventListener('click', quit);
$('moregroups').addEventListener('click', renderMore);
applyFilters();
})();
</script>
</body>
</html>
"""


if __name__ == "__main__":
    multiprocessing.freeze_support()
    sys.exit(main())
