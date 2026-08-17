"""
3GPP Specification Diff Tool - Backend API Server
"""
import json
import gzip
import os
import time
import threading
import logging
import hashlib
import re
import shutil
from pathlib import Path
from collections import OrderedDict
from contextlib import contextmanager
from functools import lru_cache
from concurrent.futures import ThreadPoolExecutor, as_completed
from flask import Flask, jsonify, request, send_from_directory, abort, Response

try:
    import fcntl
except ImportError:  # pragma: no cover - Gunicorn deployment is Linux-based.
    fcntl = None

from spec_fetcher import (
    CACHE_DIR,
    download_spec,
    extract_doc_paths,
    get_cached_path,
    list_cached_versions,
    list_versions,
)
from spec_parser import parse_spec
from diff_engine import diff_trees, compute_diff_stats

# Setup logging
Path("cache").mkdir(exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("cache/app.log", encoding="utf-8")
    ]
)
logger = logging.getLogger("3gpp_diff")

app = Flask(__name__, static_folder="static")
app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 3600

DIFF_CACHE_SCHEMA = 20
PARSED_CACHE_SCHEMA = 10

# ThreadPoolExecutor for background downloads & precomputations
_executor = ThreadPoolExecutor(max_workers=4)

# Thread-safe LRU Cache for computed diff results
class LRUCache:
    def __init__(self, maxsize=10):
        self.cache = OrderedDict()
        self.maxsize = maxsize
        self.lock = threading.Lock()

    def get(self, key):
        with self.lock:
            if key in self.cache:
                self.cache.move_to_end(key)
                return self.cache[key]
            return None

    def set(self, key, value):
        with self.lock:
            if key in self.cache:
                self.cache.move_to_end(key)
            self.cache[key] = value
            if len(self.cache) > self.maxsize:
                self.cache.popitem(last=False)

    def __contains__(self, key):
        with self.lock:
            return key in self.cache

    def delete(self, key):
        with self.lock:
            self.cache.pop(key, None)

_serialized_diff_cache = LRUCache(maxsize=24)
_serialized_changes_cache = LRUCache(maxsize=24)
_diff_search_cache = LRUCache(maxsize=6)
_diff_cache_dir = Path("cache") / "diffs" / f"v{DIFF_CACHE_SCHEMA}"
_parsed_cache_dir = Path("cache") / "parsed" / f"v{PARSED_CACHE_SCHEMA}"
_diff_lock_guard = threading.Lock()
_diff_locks = {}
_parsed_lock_guard = threading.Lock()
_parsed_locks = {}

# Known spec titles (fallback when not parsed)
SPEC_TITLES = {
    "23.501": "System architecture for the 5G System (5GS)",
    "23.502": "Procedures for the 5G System (5GS)",
    "23.503": "Policy Framework for the 5G System (5GS)",
    "38.300": "NR and NG-RAN Overall Description",
    "38.401": "NG-RAN; Architecture description",
    "38.304": "NR; UE Procedures in Idle/Inactive States",
    "33.501": "Security architecture and procedures for 5G",
}

# Background download tracking
_download_progress = {}
_download_active = set()
_task_state_lock = threading.Lock()
_task_status_dir = Path("cache") / "tasks"
_task_reservations = {}

_SPEC_ID_PATTERN = re.compile(r"^\d{2,3}\.\d{3}$")
_VERSION_PATTERN = re.compile(r"^\d{1,2}\.\d{1,2}\.\d{1,2}$")


def _valid_spec_id(spec: str) -> bool:
    return bool(_SPEC_ID_PATTERN.fullmatch(spec))


def _valid_version(version: str) -> bool:
    return bool(_VERSION_PATTERN.fullmatch(version))


def _task_status_path(kind: str, spec: str) -> Path:
    return _task_status_dir / f"{kind}-{spec}.json"


def _set_task_status(kind: str, spec: str, status: dict, memory: dict):
    """Publish task progress atomically for every Gunicorn worker to read."""
    payload = {**status, "updated_at": time.time(), "worker_pid": os.getpid()}
    memory[spec] = payload
    path = _task_status_path(kind, spec)
    temp_path = path.with_name(
        f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp"
    )
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp_path.write_text(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
        os.replace(temp_path, path)
    except OSError as exc:
        logger.warning("Unable to persist %s task status for %s: %s", kind, spec, exc)
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            pass
    return payload


def _task_has_process_lock(kind: str, spec: str) -> bool:
    if fcntl is None:
        return False
    try:
        _task_status_dir.mkdir(parents=True, exist_ok=True)
        with open(_task_lock_path(kind, spec), "a+") as handle:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except OSError:
        return False
    return False


def _read_task_status(
    kind: str,
    spec: str,
    memory: dict,
    active: set = None,
) -> dict:
    path = _task_status_path(kind, spec)
    status = None
    try:
        if path.is_file():
            candidate = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(candidate, dict) and isinstance(candidate.get("status"), str):
                status = candidate
    except (OSError, ValueError, TypeError) as exc:
        logger.warning("Unable to read %s task status for %s: %s", kind, spec, exc)
    if status is None:
        status = memory.get(spec, {"status": "not_found"})

    running_states = {"queued", "listing", "downloading", "computing"}
    if (
        active is not None
        and status.get("status") in running_states
        and spec not in active
        and not _task_has_process_lock(kind, spec)
    ):
        return _set_task_status(
            kind,
            spec,
            {
                **status,
                "status": "error",
                "error": "Background worker stopped before the task completed",
            },
            memory,
        )
    return status


def _task_lock_path(kind: str, spec: str) -> Path:
    return _task_status_dir / f"{kind}-{spec}.lock"


def _reserve_background_task(kind: str, spec: str, active: set):
    """Reserve one background task across threads and Gunicorn workers."""
    key = (kind, spec)
    with _task_state_lock:
        if spec in active:
            return None
        try:
            _task_status_dir.mkdir(parents=True, exist_ok=True)
            handle = open(_task_lock_path(kind, spec), "a+", encoding="utf-8")
            if fcntl is not None:
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    handle.close()
                    return None
        except OSError as exc:
            # A read-only task directory must not disable background work in a
            # single-worker deployment; retain the existing in-process guard.
            logger.warning("Unable to create cross-process %s lock for %s: %s", kind, spec, exc)
            handle = None
        active.add(spec)
        _task_reservations[key] = (handle, active)
        return handle if handle is not None else key


def _release_background_task(kind: str, spec: str, active: set, reservation=None):
    with _task_state_lock:
        if reservation is None:
            active.discard(spec)
            return
        current = _task_reservations.get((kind, spec))
        if current is None:
            active.discard(spec)
            return
        handle, current_active = current
        expected = handle if handle is not None else (kind, spec)
        if reservation != expected:
            return
        _task_reservations.pop((kind, spec), None)
        current_active.discard(spec)
    if handle is not None:
        try:
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


def _release_all_task_reservations():
    """Test/shutdown helper that closes every locally held task lock."""
    with _task_state_lock:
        reservations = list(_task_reservations.items())
    for (kind, spec), (handle, active) in reservations:
        _release_background_task(
            kind,
            spec,
            active,
            handle if handle is not None else (kind, spec),
        )


def _diff_cache_path(spec, v1, v2):
    """Get filesystem path for a cached diff result."""
    return _diff_cache_dir / spec / f"{v1}_to_{v2}.json.gz"


def _diff_changes_cache_path(spec, v1, v2):
    return _diff_cache_dir / spec / f"{v1}_to_{v2}.changes.json.gz"


def _diff_exists_on_disk(spec, v1, v2):
    """Check cache coverage without parsing multi-megabyte JSON payloads."""
    cache_path = _diff_cache_path(spec, v1, v2)
    return cache_path.is_file() and not _diff_sources_are_newer(cache_path, spec, v1, v2)


def _diff_sources_are_newer(cache_path: Path, spec: str, v1: str, v2: str) -> bool:
    """Detect source updates without decoding the potentially large diff."""
    try:
        cache_mtime = cache_path.stat().st_mtime_ns
    except OSError:
        return False

    for version in (v1, v2):
        candidates = [get_cached_path(spec, version)]
        extract_dir = CACHE_DIR / spec.replace(".", "_") / version
        try:
            candidates.extend(
                path for path in extract_dir.iterdir()
                if path.suffix.lower() in (".doc", ".docx")
            )
        except OSError:
            pass
        for source_path in candidates:
            try:
                if source_path.stat().st_mtime_ns > cache_mtime:
                    return True
            except OSError:
                continue
    return False


def _save_diff_to_disk(spec, v1, v2, data):
    """Serialize a diff once, then save its compressed representation."""
    cache_path = _diff_cache_path(spec, v1, v2)
    data["_cache_schema"] = DIFF_CACHE_SCHEMA
    raw = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    compressed = gzip.compress(raw, compresslevel=5, mtime=0)
    temp_path = cache_path.with_name(
        f".{cache_path.name}.{os.getpid()}.{threading.get_ident()}.tmp"
    )
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        with open(temp_path, "wb") as f:
            f.write(compressed)
        os.replace(temp_path, cache_path)
        _evict_disk_cache(spec)
    except Exception as exc:
        logger.warning("Unable to write diff cache %s: %s", cache_path, exc)
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            pass
    cache_key = _diff_cache_key(spec, v1, v2)
    _serialized_diff_cache.set(cache_key, compressed)
    _serialized_changes_cache.delete(cache_key)
    _diff_search_cache.delete(cache_key)
    return compressed


def _diff_cache_key(spec: str, v1: str, v2: str) -> str:
    return f"{spec}@{v1}→{v2}"


def _parsed_cache_path(spec: str, version: str) -> Path:
    return _parsed_cache_dir / spec / f"{version}.json.gz"


def _path_size(path: Path) -> int:
    """Best-effort byte size for cache cleanup reporting."""
    try:
        if path.is_file():
            return path.stat().st_size
        return sum(
            item.stat().st_size
            for item in path.rglob("*")
            if item.is_file()
        )
    except OSError:
        return 0


def _prune_schema_root(current_dir: Path, retain: int, *, remove_legacy_specs=False):
    root = current_dir.parent
    current_match = re.fullmatch(r"v(\d+)", current_dir.name)
    if current_match is None or not root.is_dir():
        return 0, 0
    current_schema = int(current_match.group(1))
    schema_dirs = []
    for path in root.iterdir():
        match = re.fullmatch(r"v(\d+)", path.name)
        if path.is_dir() and match:
            schema_dirs.append((int(match.group(1)), path))
    eligible = sorted(
        (schema, path) for schema, path in schema_dirs if schema <= current_schema
    )
    keep = {schema for schema, _path in eligible[-retain:]}
    targets = [path for schema, path in eligible if schema not in keep]
    if remove_legacy_specs:
        targets.extend(
            path
            for path in root.iterdir()
            if path.is_dir() and _SPEC_ID_PATTERN.fullmatch(path.name)
        )

    removed_bytes = 0
    removed_paths = 0
    for target in targets:
        removed_bytes += _path_size(target)
        try:
            shutil.rmtree(target)
            removed_paths += 1
        except OSError as exc:
            logger.warning("Unable to remove obsolete cache %s: %s", target, exc)
    return removed_paths, removed_bytes


def prune_obsolete_caches(retain: int = None) -> dict:
    """Remove recomputable cache schemas no longer read by this deployment."""
    if retain is None:
        try:
            retain = int(os.environ.get("CACHE_SCHEMA_RETENTION", "2"))
        except ValueError:
            retain = 2
    retain = max(1, min(retain, 5))
    diff_paths, diff_bytes = _prune_schema_root(
        _diff_cache_dir,
        retain,
        remove_legacy_specs=True,
    )
    parsed_paths, parsed_bytes = _prune_schema_root(_parsed_cache_dir, retain)
    result = {
        "paths": diff_paths + parsed_paths,
        "bytes": diff_bytes + parsed_bytes,
    }
    if result["paths"]:
        logger.info(
            "Removed %s obsolete cache directories (%0.1f MiB)",
            result["paths"],
            result["bytes"] / (1024 * 1024),
        )
    return result


def _source_fingerprint(doc_paths) -> list[dict]:
    paths = list(doc_paths) if isinstance(doc_paths, (list, tuple)) else [doc_paths]
    fingerprint = []
    for doc_path in paths:
        stat = doc_path.stat()
        fingerprint.append({
            "name": doc_path.name,
            "size": stat.st_size,
            "mtime_ns": stat.st_mtime_ns,
        })
    return fingerprint


def _load_parsed_from_disk(spec: str, version: str, doc_paths):
    """Restore a parsed tree when it still belongs to the current source file."""
    cache_path = _parsed_cache_path(spec, version)
    if not cache_path.is_file():
        return None
    try:
        wrapper = json.loads(gzip.decompress(cache_path.read_bytes()))
        if wrapper.get("schema") != PARSED_CACHE_SCHEMA:
            return None
        if wrapper.get("source") != _source_fingerprint(doc_paths):
            return None
        document = wrapper.get("document")
        if not isinstance(document, dict) or not isinstance(document.get("clauses"), list):
            raise ValueError("parsed cache has no clause tree")
        return document
    except Exception as exc:
        logger.warning("Unable to read parsed cache %s: %s", cache_path, exc)
        return None


def _save_parsed_to_disk(spec: str, version: str, doc_paths, document: dict):
    """Atomically persist a parsed tree for reuse across server processes."""
    cache_path = _parsed_cache_path(spec, version)
    temp_path = cache_path.with_name(f".{cache_path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        wrapper = {
            "schema": PARSED_CACHE_SCHEMA,
            "source": _source_fingerprint(doc_paths),
            "document": document,
        }
        raw = json.dumps(wrapper, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        compressed = gzip.compress(raw, compresslevel=5, mtime=0)
        temp_path.write_bytes(compressed)
        os.replace(temp_path, cache_path)
    except Exception as exc:
        logger.warning("Unable to write parsed cache %s: %s", cache_path, exc)
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            pass


def _load_serialized_diff(spec: str, v1: str, v2: str):
    """Load an already encoded diff without parsing its multi-megabyte JSON."""
    cache_key = _diff_cache_key(spec, v1, v2)
    cache_path = _diff_cache_path(spec, v1, v2)
    if cache_path.is_file() and _diff_sources_are_newer(cache_path, spec, v1, v2):
        _serialized_diff_cache.delete(cache_key)
        _serialized_changes_cache.delete(cache_key)
        _diff_search_cache.delete(cache_key)
        return None, None

    compressed = _serialized_diff_cache.get(cache_key)
    if compressed is not None:
        return compressed, "memory"

    if not cache_path.is_file():
        return None, None
    try:
        compressed = cache_path.read_bytes()
        if not compressed.startswith(b"\x1f\x8b"):
            raise ValueError("invalid gzip header")
        _serialized_diff_cache.set(cache_key, compressed)
        return compressed, "disk"
    except Exception as exc:
        logger.warning("Unable to read diff cache %s: %s", cache_path, exc)
        return None, None


def _build_serialized_changes_view(compressed: bytes) -> bytes:
    """Derive the smaller initial UI payload from a full serialized diff."""
    data = json.loads(gzip.decompress(compressed))

    def compact_node(node):
        compact = dict(node)
        compact["children"] = [
            compact_node(child) for child in node.get("children", [])
        ]
        if node.get("status") == "unchanged":
            compact.pop("body", None)
            compact.pop("images", None)
        return compact

    view = dict(data)
    view["view"] = "changes"
    view["clauses"] = [compact_node(node) for node in data.get("clauses", [])]
    raw = json.dumps(view, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return gzip.compress(raw, compresslevel=5, mtime=0)


def _build_diff_search_index(compressed: bytes) -> tuple[str, ...]:
    """Build flat, case-insensitive search text in frontend clause order."""
    data = json.loads(gzip.decompress(compressed))
    records = []
    searchable_fields = (
        "id",
        "title",
        "old_id",
        "old_title",
        "body",
        "old_body",
        "new_body",
    )

    def walk(nodes):
        for node in nodes:
            fields = [
                value
                for field in searchable_fields
                if isinstance((value := node.get(field)), str) and value
            ]
            records.append("\n".join(fields).casefold())
            walk(node.get("children", []))

    walk(data.get("clauses", []))
    return tuple(records)


def _diff_search_index(cache_key: str, compressed: bytes) -> tuple[str, ...]:
    cached = _diff_search_cache.get(cache_key)
    if cached is not None:
        return cached
    # Share the relatively expensive first JSON decode across simultaneous
    # users searching the same comparison.
    with _single_diff_computation(cache_key, cross_process=False):
        cached = _diff_search_cache.get(cache_key)
        if cached is not None:
            return cached
        index = _build_diff_search_index(compressed)
        _diff_search_cache.set(cache_key, index)
        return index


def _serialized_changes_view(
    spec: str,
    v1: str,
    v2: str,
    cache_key: str,
    compressed: bytes,
) -> bytes:
    """Build and cache the initial UI view without unchanged clause bodies."""
    cached = _serialized_changes_cache.get(cache_key)
    if cached is not None:
        return cached

    full_path = _diff_cache_path(spec, v1, v2)
    view_path = _diff_changes_cache_path(spec, v1, v2)
    try:
        if (
            view_path.is_file()
            and full_path.is_file()
            and view_path.stat().st_mtime_ns >= full_path.stat().st_mtime_ns
        ):
            serialized = view_path.read_bytes()
            if not serialized.startswith(b"\x1f\x8b"):
                raise ValueError("invalid changes-view gzip header")
            _serialized_changes_cache.set(cache_key, serialized)
            return serialized
    except Exception as exc:
        logger.warning("Unable to read changes-view cache %s: %s", view_path, exc)

    serialized = _build_serialized_changes_view(compressed)
    _serialized_changes_cache.set(cache_key, serialized)
    temp_path = view_path.with_name(
        f".{view_path.name}.{os.getpid()}.{threading.get_ident()}.tmp"
    )
    try:
        view_path.parent.mkdir(parents=True, exist_ok=True)
        temp_path.write_bytes(serialized)
        os.replace(temp_path, view_path)
    except Exception as exc:
        logger.warning("Unable to write changes-view cache %s: %s", view_path, exc)
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            pass
    return serialized


def _diff_response_for_view(
    spec: str,
    v1: str,
    v2: str,
    compressed: bytes,
    cache_source: str,
    view: str,
    *,
    refresh: bool = False,
):
    if view == "changes":
        compressed = _serialized_changes_view(
            spec,
            v1,
            v2,
            _diff_cache_key(spec, v1, v2),
            compressed,
        )
    return _serialized_diff_response(
        compressed,
        cache_source,
        refresh=refresh,
    )


def _serialized_diff_response(compressed: bytes, cache_source: str, *, refresh=False):
    """Return cached JSON bytes, preserving gzip when the client supports it."""
    etag = hashlib.sha256(compressed).hexdigest()
    if not refresh and request.if_none_match.contains(etag):
        response = Response(status=304)
        response.set_etag(etag)
        response.headers["Vary"] = "Accept-Encoding"
        response.headers["X-Diff-Cache"] = cache_source
        response.headers["Cache-Control"] = "private, no-cache"
        return response

    accepts_gzip = "gzip" in request.headers.get("Accept-Encoding", "").lower()
    payload = compressed if accepts_gzip else gzip.decompress(compressed)
    response = Response(payload, mimetype="application/json")
    if accepts_gzip:
        response.headers["Content-Encoding"] = "gzip"
    response.headers["Vary"] = "Accept-Encoding"
    response.headers["X-Diff-Cache"] = cache_source
    response.headers["Cache-Control"] = "no-store" if refresh else "private, no-cache"
    response.set_etag(etag)
    return response


@contextmanager
def _single_diff_computation(cache_key: str, *, cross_process=True):
    """Ensure simultaneous misses share one computation across web workers."""
    with _diff_lock_guard:
        entry = _diff_locks.get(cache_key)
        if entry is None:
            entry = {"lock": threading.Lock(), "users": 0}
            _diff_locks[cache_key] = entry
        entry["users"] += 1
    try:
        with entry["lock"]:
            process_lock = None
            if cross_process and fcntl is not None:
                try:
                    lock_dir = _diff_cache_dir / ".locks"
                    lock_dir.mkdir(parents=True, exist_ok=True)
                    lock_name = hashlib.sha256(cache_key.encode("utf-8")).hexdigest()
                    process_lock = open(lock_dir / f"{lock_name}.lock", "a+")
                    fcntl.flock(process_lock.fileno(), fcntl.LOCK_EX)
                except OSError as exc:
                    logger.warning(
                        "Unable to acquire cross-process diff lock for %s: %s",
                        cache_key,
                        exc,
                    )
                    if process_lock is not None:
                        process_lock.close()
                        process_lock = None
            try:
                yield
            finally:
                if process_lock is not None:
                    try:
                        fcntl.flock(process_lock.fileno(), fcntl.LOCK_UN)
                    finally:
                        process_lock.close()
    finally:
        with _diff_lock_guard:
            entry["users"] -= 1
            if entry["users"] == 0:
                _diff_locks.pop(cache_key, None)


def _evict_disk_cache(spec: str, max_per_spec: int = 30):
    """Remove oldest cached diffs for a spec if over limit."""
    spec_dir = _diff_cache_dir / spec
    if not spec_dir.exists():
        return
    files = sorted(
        (
            path for path in spec_dir.glob("*.json.gz")
            if not path.name.endswith(".changes.json.gz")
        ),
        key=lambda f: f.stat().st_mtime,
    )
    if len(files) > max_per_spec:
        for f in files[:len(files) - max_per_spec]:
            f.unlink(missing_ok=True)
            f.with_name(f.name.removesuffix(".json.gz") + ".changes.json.gz").unlink(
                missing_ok=True
            )


@app.after_request
def compress_large_json(response):
    """Compress large API payloads when supported by the browser."""
    accepts_gzip = "gzip" in request.headers.get("Accept-Encoding", "").lower()
    is_json = response.mimetype == "application/json"
    if (
        accepts_gzip
        and is_json
        and not response.is_streamed
        and 200 <= response.status_code < 300
        and "Content-Encoding" not in response.headers
    ):
        payload = response.get_data()
        if len(payload) >= 1024:
            compressed = gzip.compress(payload, compresslevel=5, mtime=0)
            if len(compressed) < len(payload):
                response.set_data(compressed)
                response.headers["Content-Encoding"] = "gzip"
                response.headers["Content-Length"] = str(len(compressed))
                response.headers["Vary"] = "Accept-Encoding"
    return response


@app.route("/")
def index():
    """Serve the frontend."""
    response = send_from_directory(app.static_folder, "index.html")
    # The HTML shell must revalidate so deployments can point at the latest
    # JS/CSS, while those larger static assets may remain cached for an hour.
    response.cache_control.max_age = 0
    response.cache_control.no_cache = True
    return response


@app.route("/api/specs")
def api_specs():
    """List specs with locally cached ZIPs."""
    specs = []
    if CACHE_DIR.exists():
        for d in sorted(CACHE_DIR.iterdir()):
            if d.is_dir() and "_" in d.name and any(f.suffix == ".zip" for f in d.iterdir()):
                spec_id = d.name.replace("_", ".")
                specs.append({
                    "id": spec_id,
                    "title": SPEC_TITLES.get(spec_id, f"TS {spec_id}"),
                })
    return jsonify(specs)


@app.route("/api/versions")
def api_versions():
    """List locally cached versions for a spec."""
    spec = request.args.get("spec", "")
    if not spec:
        return jsonify([])
    if not _valid_spec_id(spec):
        return jsonify({"error": "invalid spec number"}), 400
    try:
        versions = list_cached_versions(spec)
        for v in versions:
            rel = v.get("release", 0)
            ver = v["version"]
            if ver.endswith(".0.0"):
                v["label"] = f"Rel-{rel} ({ver})"
            else:
                v["label"] = f"Rel-{rel} maintenance ({ver})"
        versions.sort(
            key=lambda item: tuple(int(part) for part in item["version"].split(".")),
            reverse=True,
        )
        return jsonify(versions)
    except Exception as e:
        return jsonify({"error": str(e), "versions": []}), 500


def _ver_cmp(v1: str, v2: str) -> int:
    """Compare two version strings like '16.18.0' and '16.6.0'."""
    p1 = [int(x) for x in v1.split(".")]
    p2 = [int(x) for x in v2.split(".")]
    for a, b in zip(p1, p2):
        if a != b:
            return a - b
    return len(p1) - len(p2)


def _source_cache_key(doc_paths) -> tuple:
    """Build a hashable source identity for the in-process parsed LRU."""
    key = []
    for path in doc_paths:
        stat = path.stat()
        key.append((str(path), stat.st_size, stat.st_mtime_ns))
    return tuple(key)


@lru_cache(maxsize=8)
def _get_parsed_cached(spec: str, version: str, source_key: tuple) -> dict:
    """Get parsed spec (cached with LRU)."""
    doc_paths = [Path(item[0]) for item in source_key]
    cached = _load_parsed_from_disk(spec, version, doc_paths)
    if cached is not None:
        return cached
    source = doc_paths[0] if len(doc_paths) == 1 else doc_paths
    parsed = parse_spec(source, spec_number=spec, version=version)
    _save_parsed_to_disk(spec, version, doc_paths, parsed)
    return parsed


def _get_parsed(spec: str, version: str) -> dict:
    """Get a parsed spec while coalescing concurrent cold-cache requests."""
    cache_key = (spec, version)
    with _parsed_lock_guard:
        entry = _parsed_locks.get(cache_key)
        if entry is None:
            entry = {"lock": threading.Lock(), "users": 0}
            _parsed_locks[cache_key] = entry
        entry["users"] += 1
    try:
        with entry["lock"]:
            doc_paths = extract_doc_paths(spec, version)
            return _get_parsed_cached(spec, version, _source_cache_key(doc_paths))
    finally:
        with _parsed_lock_guard:
            entry["users"] -= 1
            if entry["users"] == 0:
                _parsed_locks.pop(cache_key, None)


@app.route("/api/parse")
def api_parse():
    """Parse and return a spec version's clause structure."""
    spec = request.args.get("spec", "23.501")
    version = request.args.get("version", "")

    if not version:
        return jsonify({"error": "version required"}), 400
    if not _valid_spec_id(spec) or not _valid_version(version):
        return jsonify({"error": "invalid spec or version"}), 400

    try:
        parsed = _get_parsed(spec, version)
        return jsonify(parsed)
    except Exception as exc:
        logger.exception("Unable to parse %s v%s", spec, version)
        return jsonify({"error": str(exc)}), 500


@app.route("/api/diff")
def api_diff():
    """Compute diff between two versions of a spec.
    Returns diff tree with status per clause.
    Results are cached; pass ?refresh=1 to force recompute.
    """
    spec = request.args.get("spec", "23.501")
    v1 = request.args.get("v1", "")
    v2 = request.args.get("v2", "")
    refresh = request.args.get("refresh", "0") == "1"
    view = request.args.get("view", "full")

    if not v1 or not v2:
        return jsonify({"error": "v1 and v2 required"}), 400
    if not _valid_spec_id(spec) or not _valid_version(v1) or not _valid_version(v2):
        return jsonify({"error": "invalid spec or version"}), 400
    if view not in ("full", "changes"):
        return jsonify({"error": "invalid diff view"}), 400

    cache_key = _diff_cache_key(spec, v1, v2)

    if not refresh:
        compressed, source = _load_serialized_diff(spec, v1, v2)
        if compressed is not None:
            return _diff_response_for_view(spec, v1, v2, compressed, source, view)

    # Recheck after acquiring the pair lock: another request may have filled
    # the cache while this request was waiting.
    with _single_diff_computation(cache_key):
        if not refresh:
            compressed, source = _load_serialized_diff(spec, v1, v2)
            if compressed is not None:
                return _diff_response_for_view(spec, v1, v2, compressed, source, view)
        try:
            result = _compute_diff_result(spec, v1, v2)
            compressed = _save_diff_to_disk(spec, v1, v2, result)
            source = "refresh" if refresh else "computed"
            return _diff_response_for_view(
                spec,
                v1,
                v2,
                compressed,
                source,
                view,
                refresh=refresh,
            )
        except Exception as exc:
            logger.exception("Unable to compare %s %s to %s", spec, v1, v2)
            return jsonify({"error": str(exc)}), 500


@app.route("/api/diff-search")
def api_diff_search():
    """Search a cached full diff without sending all clause bodies to the UI."""
    spec = request.args.get("spec", "")
    v1 = request.args.get("v1", "")
    v2 = request.args.get("v2", "")
    query = request.args.get("q", "")
    if not _valid_spec_id(spec) or not _valid_version(v1) or not _valid_version(v2):
        return jsonify({"error": "invalid spec or version"}), 400
    if len(query) > 512:
        return jsonify({"error": "search query is too long"}), 400

    keywords = [part.strip().casefold() for part in query.split(",") if part.strip()]
    if len(keywords) > 20 or any(len(keyword) > 128 for keyword in keywords):
        return jsonify({"error": "search query is too complex"}), 400

    compressed, source = _load_serialized_diff(spec, v1, v2)
    if compressed is None:
        return jsonify({"error": "comparison is no longer cached; refresh it"}), 409

    index = _diff_search_index(_diff_cache_key(spec, v1, v2), compressed)
    matches = [] if not keywords else [
        position
        for position, text in enumerate(index)
        if any(keyword in text for keyword in keywords)
    ]
    response = jsonify({"matches": matches, "total": len(index)})
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["X-Diff-Cache"] = source
    return response


def _compute_diff_result(spec: str, v1: str, v2: str) -> dict:
    """Parse two releases and build their complete diff response."""
    old_doc = _get_parsed(spec, v1)
    new_doc = _get_parsed(spec, v2)
    diff = diff_trees(old_doc["clauses"], new_doc["clauses"])
    return {
        "spec": spec,
        "old_version": v1,
        "new_version": v2,
        "old_release": old_doc.get("release", 0),
        "new_release": new_doc.get("release", 0),
        "title": new_doc.get("title", old_doc.get("title", "")),
        "stats": compute_diff_stats(diff),
        "clauses": diff,
    }


@app.route("/api/diff-stream")
def api_diff_stream():
    """Compute diff with streaming progress via Server-Sent Events.

    Events:
      event:progress  data:<message>
      event:done      data:<full JSON diff result>
      event:error     data:<error message>
    """
    spec = request.args.get("spec", "23.501")
    v1 = request.args.get("v1", "")
    v2 = request.args.get("v2", "")
    refresh = request.args.get("refresh", "0") == "1"
    compact = request.args.get("compact", "0") == "1"

    if not v1 or not v2:
        return jsonify({"error": "v1 and v2 required"}), 400
    if not _valid_spec_id(spec) or not _valid_version(v1) or not _valid_version(v2):
        return jsonify({"error": "invalid spec or version"}), 400

    def generate():
        cache_key = _diff_cache_key(spec, v1, v2)

        def event(name, payload):
            return f"event:{name}\ndata:{json.dumps(payload, ensure_ascii=False, separators=(',', ':'))}\n\n"

        if not refresh:
            compressed, source = _load_serialized_diff(spec, v1, v2)
            if compressed is not None:
                payload = {"ready": True, "cache": source} if compact else json.loads(gzip.decompress(compressed))
                yield event("done", payload)
                return

        yield event("progress", {"step": 0, "message": "Preparing comparison"})
        with _single_diff_computation(cache_key):
            if not refresh:
                compressed, source = _load_serialized_diff(spec, v1, v2)
                if compressed is not None:
                    payload = {"ready": True, "cache": source} if compact else json.loads(gzip.decompress(compressed))
                    yield event("done", payload)
                    return

            try:
                yield event("progress", {"step": 0, "message": f"Parsing v{v1}"})
                old_doc = _get_parsed(spec, v1)
                yield event("progress", {"step": 1, "message": f"Parsing v{v2}"})
                new_doc = _get_parsed(spec, v2)
                yield event("progress", {"step": 2, "message": "Computing clause changes"})
                diff = diff_trees(old_doc["clauses"], new_doc["clauses"])
                result = {
                    "spec": spec,
                    "old_version": v1,
                    "new_version": v2,
                    "old_release": old_doc.get("release", 0),
                    "new_release": new_doc.get("release", 0),
                    "title": new_doc.get("title", old_doc.get("title", "")),
                    "stats": compute_diff_stats(diff),
                    "clauses": diff,
                }
                _save_diff_to_disk(spec, v1, v2, result)
                payload = {"ready": True, "cache": "refresh" if refresh else "computed"} if compact else result
                yield event("done", payload)
            except Exception as exc:
                logger.exception("Streaming comparison failed for %s %s to %s", spec, v1, v2)
                yield event("error", {"message": str(exc)})

    return Response(
        generate(),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.route("/api/image/<spec>/<version>/<filename>")
def api_image(spec, version, filename):
    """Serve extracted spec images from cache."""
    if not _valid_spec_id(spec) or not _valid_version(version):
        abort(404)
    image_path = Path("cache") / "images" / spec / version / filename
    if not image_path.exists():
        abort(404)
    # Infer content type from extension
    ext = image_path.suffix.lower()
    mime_map = {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".gif": "image/gif",
        ".svg": "image/svg+xml",
        ".wmf": "image/wmf",
        ".emf": "image/emf",
        ".bmp": "image/bmp",
        ".tiff": "image/tiff",
        ".tif": "image/tiff",
    }
    mimetype = mime_map.get(ext, "application/octet-stream")
    return send_from_directory(
        image_path.parent,
        image_path.name,
        mimetype=mimetype,
        as_attachment=request.args.get("download") == "1",
        download_name=image_path.name,
        max_age=86400,
        conditional=True,
    )


# ===================== Background Download =====================

def _download_all_releases(spec: str, _reservation=None):
    """Download .0.0 versions for releases 15+ in background.
    Uses a quick FTP listing (5s timeout); falls back to known releases.
    """
    _set_task_status(
        "download",
        spec,
        {"status": "listing", "total": 0, "done": 0},
        _download_progress,
    )
    try:
        # Quick FTP listing (5s timeout) to know what's available
        try:
            versions = list_versions(spec, timeout=5)
            seen = set()
            to_download = []
            for v in versions:
                rel = v.get("release", 0)
                parts = v["version"].split(".")
                if rel >= 15 and parts[1] == "0" and parts[2] == "0" and rel not in seen:
                    seen.add(rel)
                    to_download.append(v["version"])
            if not to_download:
                raise ValueError("No releases found on FTP")
        except Exception:
            logger.info(f"[download] FTP listing slow/unavailable, trying known releases")
            to_download = [f"{r}.0.0" for r in range(15, 21)]  # Rel-15 through Rel-20

        # Download each release
        downloaded = []
        _set_task_status(
            "download",
            spec,
            {"status": "downloading", "total": len(to_download), "done": 0},
            _download_progress,
        )
        worker_count = min(3, len(to_download))
        with ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix="spec-download") as downloads:
            futures = {downloads.submit(download_spec, spec, ver): ver for ver in to_download}
            for completed, future in enumerate(as_completed(futures), start=1):
                ver = futures[future]
                try:
                    future.result()
                    downloaded.append(ver)
                except Exception as exc:
                    logger.error("[download] %s v%s not available: %s", spec, ver, exc)
                _set_task_status(
                    "download",
                    spec,
                    {
                        "status": "downloading",
                        "total": len(to_download),
                        "done": completed,
                        "versions": sorted(
                            downloaded,
                            key=lambda item: tuple(map(int, item.split('.'))),
                        ),
                    },
                    _download_progress,
                )

        downloaded.sort(key=lambda item: tuple(map(int, item.split('.'))))
        failed = len(to_download) - len(downloaded)
        _set_task_status(
            "download",
            spec,
            {
                "status": "completed",
                "total": len(to_download),
                "done": len(to_download),
                "available": len(downloaded),
                "failed": failed,
                "versions": downloaded,
            },
            _download_progress,
        )
        logger.info(
            "[download] %s: %s releases downloaded, %s unavailable",
            spec,
            len(downloaded),
            failed,
        )

        # Trigger precomputation in background now that ZIPs are available
        _submit_precompute(spec)
    except Exception as exc:
        logger.exception("Background download failed for %s", spec)
        _set_task_status(
            "download",
            spec,
            {"status": "error", "error": str(exc)},
            _download_progress,
        )
    finally:
        _release_background_task(
            "download", spec, _download_active, _reservation
        )


@app.route("/api/download", methods=["POST"])
def api_download():
    """Download all releases for a spec in background."""
    data = request.get_json(silent=True) or {}
    spec = data.get("spec", "")
    if not spec:
        return jsonify({"error": "spec required"}), 400
    if not _valid_spec_id(spec):
        return jsonify({"error": "Use a spec number such as 23.501"}), 400
    reservation = _reserve_background_task("download", spec, _download_active)
    if reservation is None:
        return jsonify({"status": "already_running", "spec": spec})
    _set_task_status(
        "download",
        spec,
        {"status": "queued", "total": 0, "done": 0},
        _download_progress,
    )
    try:
        _executor.submit(_download_all_releases, spec, reservation)
    except Exception:
        _release_background_task(
            "download", spec, _download_active, reservation
        )
        raise
    return jsonify({"status": "started", "spec": spec})


@app.route("/api/download-status")
def api_download_status():
    """Check download progress for a spec."""
    spec = request.args.get("spec", "")
    if not _valid_spec_id(spec):
        return jsonify({"error": "invalid spec number"}), 400
    progress = _read_task_status(
        "download", spec, _download_progress, _download_active
    )
    return jsonify(progress)


# ===================== Background Precomputation =====================

_precompute_active = set()  # specs currently being precomputed
_precompute_status = {}     # spec → {status, total, done, pairs}


def _precompute_diffs(spec="23.501", max_releases=6, _reservation=None):
    """Compute diffs between cached release pairs (full mesh) in background."""
    try:
        cached = list_cached_versions(spec)
        base_versions = {
            item["release"]: item["version"]
            for item in cached
            if item.get("release", 0) >= 15 and item["version"].endswith(".0.0")
        }
        releases = sorted(base_versions)[-max_releases:]

        pairs = []
        for i in range(len(releases)):
            for j in range(i + 1, len(releases)):
                pairs.append((base_versions[releases[i]], base_versions[releases[j]]))
        # The UI defaults to the two newest adjacent releases. Make that pair
        # usable first, then finish other adjacent comparisons before the less
        # common long-range mesh.
        pairs.sort(key=lambda pair: (
            0 if int(pair[1].split(".", 1)[0]) - int(pair[0].split(".", 1)[0]) == 1 else 1,
            -int(pair[1].split(".", 1)[0]),
            int(pair[1].split(".", 1)[0]) - int(pair[0].split(".", 1)[0]),
        ))

        to_compute = []
        already_done = 0
        for v1, v2 in pairs:
            cache_key = f"{spec}@{v1}→{v2}"
            if cache_key in _serialized_diff_cache or _diff_exists_on_disk(spec, v1, v2):
                already_done += 1
            else:
                to_compute.append((v1, v2))

        _set_task_status(
            "precompute",
            spec,
            {
                "status": "computing",
                "total": len(pairs),
                "done": already_done,
                "processed": already_done,
                "failed": 0,
                "pending": len(to_compute),
            },
            _precompute_status,
        )

        if not to_compute:
            logger.info(f"[precompute] {spec}: all {len(pairs)} diffs already cached")
            _set_task_status(
                "precompute",
                spec,
                {**_precompute_status[spec], "status": "completed"},
                _precompute_status,
            )
            return

        logger.info(f"[precompute] Will compute {len(to_compute)} diffs for {spec} ({already_done} already cached)")
        ready = already_done
        processed = already_done
        failures = []
        for v1, v2 in to_compute:
            cache_key = _diff_cache_key(spec, v1, v2)
            try:
                # Coordinate with interactive API/SSE requests. After waiting,
                # recheck the cache so the background job never repeats work a
                # foreground request has already completed.
                with _single_diff_computation(cache_key):
                    compressed, _source = _load_serialized_diff(spec, v1, v2)
                    if compressed is None:
                        result = _compute_diff_result(spec, v1, v2)
                        _save_diff_to_disk(spec, v1, v2, result)
                    else:
                        result = None
                ready += 1
                if result is None:
                    logger.info("[precompute] %s → %s became ready concurrently", v1, v2)
                else:
                    stats = result["stats"]
                    logger.info(f"[precompute] ✓ {v1} → {v2} ({stats['modified']} modified, {stats['added']} added, {stats['deleted']} deleted)")
            except Exception as exc:
                failures.append({"v1": v1, "v2": v2, "error": str(exc)})
                logger.error("[precompute] ✗ %s → %s: %s", v1, v2, exc)
            processed += 1
            _set_task_status(
                "precompute",
                spec,
                {
                    "status": "computing",
                    "total": len(pairs),
                    "done": ready,
                    "processed": processed,
                    "failed": len(failures),
                    "pending": len(pairs) - processed,
                    "failures": failures[-10:],
                },
                _precompute_status,
            )
            # Yield GIL so Flask request threads can make progress
            time.sleep(0.001)
        final_status = "partial" if failures else "completed"
        _set_task_status(
            "precompute",
            spec,
            {**_precompute_status[spec], "status": final_status},
            _precompute_status,
        )
        logger.info(
            "[precompute] %s: %s (%s/%s ready, %s failed)",
            spec,
            final_status,
            ready,
            len(pairs),
            len(failures),
        )
    except Exception as exc:
        logger.exception("Background precomputation failed for %s", spec)
        _set_task_status(
            "precompute",
            spec,
            {"status": "error", "error": str(exc)},
            _precompute_status,
        )
    finally:
        _release_background_task(
            "precompute", spec, _precompute_active, _reservation
        )


def _submit_precompute(spec: str) -> bool:
    """Reserve and submit one precompute task per specification."""
    reservation = _reserve_background_task(
        "precompute", spec, _precompute_active
    )
    if reservation is None:
        return False
    _set_task_status(
        "precompute",
        spec,
        {"status": "queued", "total": 0, "done": 0},
        _precompute_status,
    )
    try:
        _executor.submit(_precompute_diffs, spec, 6, reservation)
        return True
    except Exception:
        _release_background_task(
            "precompute", spec, _precompute_active, reservation
        )
        raise


@app.route("/api/precompute", methods=["POST"])
def api_precompute():
    """Trigger precomputation for a spec in background."""
    data = request.get_json(silent=True) or {}
    spec = data.get("spec", "")
    if not spec:
        return jsonify({"error": "spec required"}), 400
    if not _valid_spec_id(spec):
        return jsonify({"error": "invalid spec number"}), 400
    if not _submit_precompute(spec):
        return jsonify({"status": "already_running", "spec": spec})
    return jsonify({"status": "started", "spec": spec})


@app.route("/api/precompute-status")
def api_precompute_status():
    """Check precomputation status for a spec."""
    spec = request.args.get("spec", "")
    if not _valid_spec_id(spec):
        return jsonify({"error": "invalid spec number"}), 400
    status = _read_task_status(
        "precompute", spec, _precompute_status, _precompute_active
    )
    return jsonify(status)


@app.route("/api/diff-coverage")
def api_diff_coverage():
    """Check which diffs exist vs missing for a spec."""
    spec = request.args.get("spec", "")
    if not spec:
        return jsonify({"error": "spec required"}), 400
    if not _valid_spec_id(spec):
        return jsonify({"error": "invalid spec number"}), 400

    cached = list_cached_versions(spec)
    base_versions = {
        item["release"]: item["version"]
        for item in cached
        if item.get("release", 0) >= 15 and item["version"].endswith(".0.0")
    }
    releases = sorted(base_versions)

    pairs = []
    for i in range(len(releases)):
        for j in range(i + 1, len(releases)):
            v1 = base_versions[releases[i]]
            v2 = base_versions[releases[j]]
            cache_key = f"{spec}@{v1}→{v2}"
            on_memory = cache_key in _serialized_diff_cache
            on_disk = _diff_exists_on_disk(spec, v1, v2)
            pairs.append({
                "v1": v1, "v2": v2,
                "cached": on_memory or on_disk,
                "source": "memory" if on_memory else ("disk" if on_disk else "missing"),
            })

    total = len(pairs)
    cached_count = sum(1 for p in pairs if p["cached"])
    return jsonify({
        "spec": spec,
        "releases": releases,
        "total": total,
        "cached": cached_count,
        "missing": total - cached_count,
        "coverage": f"{cached_count}/{total}",
        "pairs": pairs,
    })


if __name__ == "__main__":
    # Create cache directory
    Path("cache").mkdir(exist_ok=True)
    prune_obsolete_caches()

    port = int(os.environ.get("PORT", 5001))
    logger.info(f"3GPP Diff Tool starting on http://0.0.0.0:{port}")
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
