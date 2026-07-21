"""
3GPP Specification Diff Tool - Backend API Server
"""
import json
import gzip
import os
import time
import threading
import logging
from pathlib import Path
from collections import OrderedDict
from functools import lru_cache
from concurrent.futures import ThreadPoolExecutor
from flask import Flask, jsonify, request, send_from_directory, abort, Response

from spec_fetcher import list_versions, extract_doc_path, list_cached_versions, download_spec, CACHE_DIR
from spec_parser import parse_spec, clause_count
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

DIFF_CACHE_SCHEMA = 3

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

_diff_cache = LRUCache(maxsize=20)
_diff_cache_dir = Path("cache") / "diffs" / f"v{DIFF_CACHE_SCHEMA}"

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


def _diff_cache_path(spec, v1, v2):
    """Get filesystem path for a cached diff result."""
    return _diff_cache_dir / spec / f"{v1}_to_{v2}.json"


def _diff_exists_on_disk(spec, v1, v2):
    """Check cache coverage without parsing multi-megabyte JSON payloads."""
    return _diff_cache_path(spec, v1, v2).is_file()


def _load_diff_from_disk(spec, v1, v2):
    """Load diff result from disk cache if available."""
    cache_path = _diff_cache_path(spec, v1, v2)
    if cache_path.exists():
        try:
            with open(cache_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if _normalize_diff_cache(data):
                _save_diff_to_disk(spec, v1, v2, data)
            return data
        except Exception as exc:
            logger.warning("Unable to read diff cache %s: %s", cache_path, exc)
    return None


def _save_diff_to_disk(spec, v1, v2, data):
    """Save diff result to disk cache."""
    cache_path = _diff_cache_path(spec, v1, v2)
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    data["_cache_schema"] = DIFF_CACHE_SCHEMA
    temp_path = cache_path.with_name(f".{cache_path.name}.{threading.get_ident()}.tmp")
    try:
        with open(temp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
        os.replace(temp_path, cache_path)
    except Exception as exc:
        logger.warning("Unable to write diff cache %s: %s", cache_path, exc)
        temp_path.unlink(missing_ok=True)
    _evict_disk_cache(spec)


def _normalize_diff_cache(data: dict) -> bool:
    """Migrate legacy cache payloads without recomputing their diffs."""
    changed = data.get("_cache_schema") != DIFF_CACHE_SCHEMA
    stack = list(data.get("clauses", []))
    while stack:
        node = stack.pop()
        for legacy_key in ("old_body_lines", "new_body_lines", "_sort_key"):
            if legacy_key in node:
                node.pop(legacy_key, None)
                changed = True
        stack.extend(node.get("children", []))
    data["_cache_schema"] = DIFF_CACHE_SCHEMA
    return changed


def _evict_disk_cache(spec: str, max_per_spec: int = 30):
    """Remove oldest cached diffs for a spec if over limit."""
    spec_dir = _diff_cache_dir / spec
    if not spec_dir.exists():
        return
    files = sorted(spec_dir.glob("*.json"), key=lambda f: f.stat().st_mtime)
    if len(files) > max_per_spec:
        for f in files[:len(files) - max_per_spec]:
            f.unlink(missing_ok=True)


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
    return send_from_directory(app.static_folder, "index.html")


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
    try:
        versions = list_cached_versions(spec)
        for v in versions:
            rel = v.get("release", 0)
            ver = v["version"]
            if ver.endswith(".0.0"):
                v["label"] = f"Rel-{rel} ({ver})"
            else:
                v["label"] = f"Rel-{rel} maintenance ({ver})"
        versions.sort(key=lambda x: x["release"], reverse=True)
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


@lru_cache(maxsize=16)
def _get_parsed(spec: str, version: str) -> dict:
    """Get parsed spec (cached with LRU)."""
    doc_path = extract_doc_path(spec, version)
    parsed = parse_spec(doc_path, spec_number=spec, version=version)
    return parsed


@app.route("/api/parse")
def api_parse():
    """Parse and return a spec version's clause structure."""
    spec = request.args.get("spec", "23.501")
    version = request.args.get("version", "")

    if not version:
        return jsonify({"error": "version required"}), 400

    try:
        parsed = _get_parsed(spec, version)
        return jsonify(parsed)
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


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

    if not v1 or not v2:
        return jsonify({"error": "v1 and v2 required"}), 400

    cache_key = f"{spec}@{v1}→{v2}"

    if not refresh:
        # Normal request: memory → disk → compute
        cached_result = _diff_cache.get(cache_key)
        if cached_result is not None:
            return jsonify(cached_result)
        disk_result = _load_diff_from_disk(spec, v1, v2)
        if disk_result is not None:
            _diff_cache.set(cache_key, disk_result)
            return jsonify(disk_result)
    # refresh=1: skip both caches, force recompute

    # No cache — compute from scratch
    try:
        old_doc = _get_parsed(spec, v1)
        new_doc = _get_parsed(spec, v2)

        diff = diff_trees(old_doc["clauses"], new_doc["clauses"])
        stats = compute_diff_stats(diff)

        result = {
            "spec": spec,
            "old_version": v1,
            "new_version": v2,
            "old_release": old_doc.get("release", 0),
            "new_release": new_doc.get("release", 0),
            "title": new_doc.get("title", old_doc.get("title", "")),
            "stats": stats,
            "clauses": diff,
        }
        _diff_cache.set(cache_key, result)
        _save_diff_to_disk(spec, v1, v2, result)
        return jsonify(result)
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


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

    if not v1 or not v2:
        return jsonify({"error": "v1 and v2 required"}), 400

    def generate():
        cache_key = f"{spec}@{v1}→{v2}"

        if not refresh:
            cached_result = _diff_cache.get(cache_key)
            if cached_result is not None:
                yield f"event:done\ndata:{json.dumps(cached_result)}\n\n"
                return
            disk_result = _load_diff_from_disk(spec, v1, v2)
            if disk_result is not None:
                _diff_cache.set(cache_key, disk_result)
                yield f"event:done\ndata:{json.dumps(disk_result)}\n\n"
                return

        yield f"event:progress\ndata:Parsing {v1}...\n\n"
        try:
            old_doc = _get_parsed(spec, v1)
        except Exception as e:
            yield f"event:error\ndata:{json.dumps(str(e))}\n\n"
            return

        yield f"event:progress\ndata:Parsing {v2}...\n\n"
        try:
            new_doc = _get_parsed(spec, v2)
        except Exception as e:
            yield f"event:error\ndata:{json.dumps(str(e))}\n\n"
            return

        yield f"event:progress\ndata:Computing diff...\n\n"
        try:
            diff = diff_trees(old_doc["clauses"], new_doc["clauses"])
            stats = compute_diff_stats(diff)
        except Exception as e:
            yield f"event:error\ndata:{json.dumps(str(e))}\n\n"
            return

        result = {
            "spec": spec,
            "old_version": v1,
            "new_version": v2,
            "old_release": old_doc.get("release", 0),
            "new_release": new_doc.get("release", 0),
            "title": new_doc.get("title", old_doc.get("title", "")),
            "stats": stats,
            "clauses": diff,
        }
        _diff_cache.set(cache_key, result)
        _save_diff_to_disk(spec, v1, v2, result)
        yield f"event:done\ndata:{json.dumps(result)}\n\n"

    return Response(
        generate(),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.route("/api/image/<spec>/<version>/<filename>")
def api_image(spec, version, filename):
    """Serve extracted spec images from cache."""
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
        ".wmf": "application/x-msmetafile",
        ".emf": "application/x-msmetafile",
        ".bmp": "image/bmp",
        ".tiff": "image/tiff",
        ".tif": "image/tiff",
    }
    mimetype = mime_map.get(ext, "application/octet-stream")
    return send_from_directory(image_path.parent, image_path.name, mimetype=mimetype)


# ===================== Background Download =====================

def _download_all_releases(spec: str):
    """Download .0.0 versions for releases 15+ in background.
    Uses a quick FTP listing (5s timeout); falls back to known releases.
    """
    if spec in _download_active:
        return
    _download_active.add(spec)
    _download_progress[spec] = {"status": "listing", "total": 0, "done": 0}
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
        _download_progress[spec] = {"status": "downloading", "total": len(to_download), "done": 0}
        for i, ver in enumerate(to_download):
            try:
                download_spec(spec, ver)
                downloaded.append(ver)
            except Exception as e:
                logger.error(f"[download] {spec} v{ver} not available: {e}")
            _download_progress[spec] = {"status": "downloading", "total": len(to_download), "done": i + 1, "versions": downloaded}

        _download_progress[spec] = {"status": "completed", "total": len(to_download), "done": len(downloaded), "versions": downloaded}
        logger.info(f"[download] ✓ {spec}: {len(downloaded)} releases downloaded")

        # Trigger precomputation in background now that ZIPs are available
        _executor.submit(_precompute_diffs, spec)
    except Exception as e:
        import traceback
        traceback.print_exc()
        _download_progress[spec] = {"status": "error", "error": str(e)}
    finally:
        _download_active.discard(spec)


@app.route("/api/download", methods=["POST"])
def api_download():
    """Download all releases for a spec in background."""
    data = request.get_json(force=True)
    spec = data.get("spec", "")
    if not spec:
        return jsonify({"error": "spec required"}), 400
    if spec in _download_active:
        return jsonify({"status": "already_running", "spec": spec})
    _executor.submit(_download_all_releases, spec)
    return jsonify({"status": "started", "spec": spec})


@app.route("/api/download-status")
def api_download_status():
    """Check download progress for a spec."""
    spec = request.args.get("spec", "")
    progress = _download_progress.get(spec, {"status": "not_found"})
    return jsonify(progress)


# ===================== Background Precomputation =====================

_precompute_active = set()  # specs currently being precomputed
_precompute_status = {}     # spec → {status, total, done, pairs}


def _precompute_diffs(spec="23.501", max_releases=6):
    """Compute diffs between cached release pairs (full mesh) in background."""
    if spec in _precompute_active:
        logger.info(f"[precompute] {spec}: already running, skipping")
        return
    _precompute_active.add(spec)
    try:
        cached = list_cached_versions(spec)
        releases = sorted(set(
            v["release"] for v in cached if v.get("release", 0) >= 15
        ))[-max_releases:]

        pairs = []
        for i in range(len(releases)):
            for j in range(i + 1, len(releases)):
                pairs.append((f"{releases[i]}.0.0", f"{releases[j]}.0.0"))

        to_compute = []
        already_done = 0
        for v1, v2 in pairs:
            cache_key = f"{spec}@{v1}→{v2}"
            if cache_key in _diff_cache or _diff_exists_on_disk(spec, v1, v2):
                already_done += 1
            else:
                to_compute.append((v1, v2))

        _precompute_status[spec] = {
            "status": "computing",
            "total": len(pairs),
            "done": already_done,
            "pending": len(to_compute),
        }

        if not to_compute:
            logger.info(f"[precompute] {spec}: all {len(pairs)} diffs already cached")
            _precompute_status[spec]["status"] = "completed"
            return

        logger.info(f"[precompute] Will compute {len(to_compute)} diffs for {spec} ({already_done} already cached)")
        for idx, (v1, v2) in enumerate(to_compute):
            cache_key = f"{spec}@{v1}→{v2}"
            try:
                old_doc = _get_parsed(spec, v1)
                new_doc = _get_parsed(spec, v2)
                diff = diff_trees(old_doc["clauses"], new_doc["clauses"])
                stats = compute_diff_stats(diff)
                result = {
                    "spec": spec,
                    "old_version": v1,
                    "new_version": v2,
                    "old_release": old_doc.get("release", 0),
                    "new_release": new_doc.get("release", 0),
                    "title": new_doc.get("title", old_doc.get("title", "")),
                    "stats": stats,
                    "clauses": diff,
                }
                _diff_cache.set(cache_key, result)
                _save_diff_to_disk(spec, v1, v2, result)
                _precompute_status[spec]["done"] = already_done + idx + 1
                logger.info(f"[precompute] ✓ {v1} → {v2} ({stats['modified']} modified, {stats['added']} added, {stats['deleted']} deleted)")
            except Exception as e:
                logger.error(f"[precompute] ✗ {v1} → {v2}: {e}")
            # Yield GIL so Flask request threads can make progress
            time.sleep(0.001)
        _precompute_status[spec]["status"] = "completed"
        logger.info(f"[precompute] {spec}: done ({_precompute_status[spec]['done']}/{len(pairs)} diffs)")
    except Exception as e:
        import traceback
        traceback.print_exc()
        _precompute_status[spec] = {"status": "error", "error": str(e)}
    finally:
        _precompute_active.discard(spec)


@app.route("/api/precompute", methods=["POST"])
def api_precompute():
    """Trigger precomputation for a spec in background."""
    data = request.get_json(force=True)
    spec = data.get("spec", "")
    if not spec:
        return jsonify({"error": "spec required"}), 400
    if spec in _precompute_active:
        return jsonify({"status": "already_running", "spec": spec})
    _executor.submit(_precompute_diffs, spec)
    return jsonify({"status": "started", "spec": spec})


@app.route("/api/precompute-status")
def api_precompute_status():
    """Check precomputation status for a spec."""
    spec = request.args.get("spec", "")
    status = _precompute_status.get(spec, {"status": "not_found"})
    return jsonify(status)


@app.route("/api/diff-coverage")
def api_diff_coverage():
    """Check which diffs exist vs missing for a spec."""
    spec = request.args.get("spec", "")
    if not spec:
        return jsonify({"error": "spec required"}), 400

    cached = list_cached_versions(spec)
    releases = sorted(set(
        v["release"] for v in cached if v.get("release", 0) >= 15
    ))

    pairs = []
    for i in range(len(releases)):
        for j in range(i + 1, len(releases)):
            v1 = f"{releases[i]}.0.0"
            v2 = f"{releases[j]}.0.0"
            cache_key = f"{spec}@{v1}→{v2}"
            on_memory = cache_key in _diff_cache
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

    port = int(os.environ.get("PORT", 5001))
    logger.info(f"3GPP Diff Tool starting on http://0.0.0.0:{port}")
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
