"""
Download and cache 3GPP specification ZIP files from the official FTP.
Handles both .doc (older) and .docx (newer) formats.
"""
import os
import re
import subprocess
import shutil
import zipfile
from pathlib import Path

CACHE_DIR = Path(__file__).parent / "cache"

# 3GPP FTP base URL for spec archives
FTP_BASE = "https://www.3gpp.org/ftp/Specs/archive"

CURL_UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"


def _curl(url: str, timeout: int = 60) -> str:
    """Fetch URL with curl (3GPP blocks python urllib)."""
    result = subprocess.run(
        ["curl", "-s", "-L", "-A", CURL_UA, "--max-time", str(timeout), url],
        capture_output=True,
        text=True,
        timeout=timeout + 10,
    )
    if result.returncode != 0:
        raise RuntimeError(f"curl failed ({result.returncode}): {result.stderr[:200]}")
    return result.stdout


def _curl_binary(url: str, outpath: Path, timeout: int = 120):
    """Download binary file with curl."""
    result = subprocess.run(
        ["curl", "-s", "-L", "-A", CURL_UA, "--max-time", str(timeout),
         "-o", str(outpath), url],
        capture_output=True,
        text=True,
        timeout=timeout + 10,
    )
    if result.returncode != 0:
        raise RuntimeError(f"curl download failed ({result.returncode}): {result.stderr[:200]}")


def _release_code(version: str) -> str:
    """Extract the release letter code from a version string like '16.0.0' -> 'g'"""
    major = version.split(".")[0]
    mapping = {
        "15": "f", "16": "g", "17": "h",
        "18": "i", "19": "j", "20": "k",
        "21": "l",
    }
    if major in ("0", "1", "2"):
        return None
    return mapping.get(major, None)


def _encode_num(n: int) -> str:
    """Encode a number in single-char base-36 style.
    0-9 -> '0'-'9', 10->'a', 11->'b', ..., 35->'z'.
    Only handles single-digit encoding (n <= 35).
    """
    if n < 10:
        return str(n)
    return chr(ord('a') + n - 10)


def _version_to_filename(spec: str, version: str) -> str:
    """Convert version to ZIP filename.
    23.501, 16.0.0 -> '23501-g00.zip'
    23.501, 16.18.0 -> '23501-gia.zip' (minor=18→i, patch=0→0)
    23.501, 15.10.0 -> '23501-fa0.zip' (minor=10→a, patch=0→0)
    """
    major, minor, patch = version.split(".")
    release = _release_code(version)
    if not release:
        # Pre-release: just use raw digits
        return f"{spec.replace('.', '')}-{major}{minor}{patch}.zip"
    minor_enc = _encode_num(int(minor))
    patch_enc = _encode_num(int(patch))
    return f"{spec.replace('.', '')}-{release}{minor_enc}{patch_enc}.zip"


def _parse_filename(spec: str, filename: str) -> dict:
    """Parse a ZIP filename into version info.
    '23501-g00.zip' -> {version: '16.0.0', release: 16, label: 'Rel-16 (16.0.0)'}
    """
    spec_num = spec.replace(".", "")
    stem = filename.replace(".zip", "").replace(spec_num + "-", "")

    rev_map = {"f": 15, "g": 16, "h": 17, "i": 18, "j": 19, "k": 20, "l": 21}

    # Letter format: g00 -> Rel-16 v16.0.0, fa0 -> Rel-15 v15.10.0
    m = re.match(r"([a-z])(\w+)$", stem)
    if m and m.group(1) in rev_map:
        letter = m.group(1)
        num_part = m.group(2)
        release = rev_map[letter]

        # Parse minor/patch from num_part:
        #   g00  -> minor=0,  patch=0  (16.0.0)
        #   g10  -> minor=1,  patch=0  (16.1.0)
        #   g11  -> minor=1,  patch=1  (16.1.1)
        #   fa0  -> minor=10, patch=0  (15.10.0) where 'a'=10
        #   fd0  -> minor=13, patch=0  (15.13.0)
        def _parse_num(s: str) -> int:
            """Parse digits where 'a'=10, 'b'=11, etc."""
            if s.isdigit():
                return int(s)
            # Hex-like: a=10, b=11, ...
            val = 0
            for ch in s:
                if ch.isdigit():
                    val = val * 10 + int(ch)
                else:
                    val = val * 10 + (ord(ch) - ord('a') + 10)
            return val

        minor = _parse_num(num_part[:-1]) if len(num_part) > 1 else 0
        patch = _parse_num(num_part[-1]) if len(num_part) > 1 else int(num_part) if num_part else 0

        version = f"{release}.{minor}.{patch}"
        return {
            "version": version,
            "release": release,
            "filename": filename,
            "label": f"Rel-{release} (v{version})",
        }

    # Numeric format: 000, 010 -> pre-release
    m = re.match(r"(\d)(\d)(\d)$", stem)
    if m:
        major, minor, patch = m.groups()
        release = 0
        version = f"{int(major)}.{int(minor)}.{int(patch)}"
        return {
            "version": version,
            "release": release,
            "filename": filename,
            "label": f"Draft v{version}",
        }

    return {"version": stem, "release": 0, "filename": filename, "label": stem}


def _list_versions_from_ftp(spec: str, timeout: int = 30) -> list[dict]:
    """List available versions from 3GPP FTP directory listing via curl."""
    series = spec.split(".")[0]
    spec_dotted = spec  # e.g. "23.501"
    spec_num = spec.replace(".", "")
    url = f"{FTP_BASE}/{series}_series/{spec_dotted}/"

    try:
        html = _curl(url, timeout=timeout)
    except Exception as e:
        print(f"Warning: FTP listing failed: {e}")
        return []

    versions = []
    # Find all zip file links (href may be full URL or relative path)
    file_pattern = re.compile(r'href="([^"]*\.zip)"')
    for match in file_pattern.finditer(html):
        href = match.group(1).strip()
        # Extract just filename from full URL
        filename = href.split("/")[-1]
        if filename.startswith(spec_num):
            versions.append(_parse_filename(spec, filename))

    # Deduplicate by version
    seen = set()
    unique = []
    for v in versions:
        vkey = v["version"]
        if vkey not in seen:
            seen.add(vkey)
            unique.append(v)

    return unique


def get_cached_path(spec: str, version: str) -> Path:
    """Get the local cached path for a spec version's zip file."""
    filename = _version_to_filename(spec, version)
    return CACHE_DIR / spec.replace(".", "_") / filename


def download_spec(spec: str, version: str) -> Path:
    """Download a spec ZIP from 3GPP FTP, cache locally, return path."""
    filename = _version_to_filename(spec, version)
    series = spec.split(".")[0]
    spec_dotted = spec
    spec_num = spec.replace(".", "")
    url = f"{FTP_BASE}/{series}_series/{spec_dotted}/{filename}"
    cache_path = get_cached_path(spec, version)
    cache_path.parent.mkdir(parents=True, exist_ok=True)

    if cache_path.exists():
        if _is_valid_zip(cache_path):
            return cache_path
        cache_path.unlink()

    print(f"Downloading {url} ...")
    _curl_binary(url, cache_path, timeout=120)

    if not _is_valid_zip(cache_path):
        cache_path.unlink()
        raise FileNotFoundError(f"Downloaded file is not a valid ZIP: {url}")

    print(f"  -> saved to {cache_path}")
    return cache_path


def _is_valid_zip(path: Path) -> bool:
    """Check if file is a valid ZIP (not an HTML error page)."""
    import zipfile
    try:
        with zipfile.ZipFile(path, "r") as zf:
            zf.namelist()
        return True
    except (zipfile.BadZipFile, OSError):
        return False


def extract_doc_path(spec: str, version: str) -> Path:
    """Download (if needed) and extract the .doc/.docx from ZIP, return its path."""
    zip_path = download_spec(spec, version)

    extract_dir = CACHE_DIR / spec.replace(".", "_") / version
    extract_dir.mkdir(parents=True, exist_ok=True)

    # Check if already extracted
    existing = list(extract_dir.glob("*.doc*"))
    if existing:
        return existing[0]

    # Extract
    with zipfile.ZipFile(zip_path, "r") as zf:
        zf.extractall(extract_dir)

    extracted = list(extract_dir.glob("*.doc*"))
    if not extracted:
        raise FileNotFoundError(f"No .doc/.docx in {zip_path}")
    return extracted[0]


def list_versions(spec: str, timeout: int = 30) -> list[dict]:
    """List all available versions for a spec."""
    return _list_versions_from_ftp(spec, timeout=timeout)


def list_cached_versions(spec: str) -> list[dict]:
    """List versions that have been downloaded and cached locally."""
    cache_dir = CACHE_DIR / spec.replace(".", "_")
    versions = []
    if cache_dir.exists():
        for f in sorted(cache_dir.iterdir()):
            if f.suffix == ".zip":
                parsed = _parse_filename(spec, f.name)
                if parsed.get("release", 0) >= 15:
                    versions.append(parsed)
    return versions


if __name__ == "__main__":
    # Test
    import json
    versions = list_versions("23.501")
    for v in versions:
        if v["release"] >= 15:
            print(f"  Rel-{v['release']}: v{v['version']}  ({v.get('label','')})")
    print(f"\nTotal: {len(versions)}")
