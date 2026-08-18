"""
Parse 3GPP Word (.doc/.docx) specification documents into structured clause trees.
"""
import re
import subprocess
import tempfile
import hashlib
import logging
import os
import shutil
import sys
import uuid
from collections import defaultdict
from pathlib import Path

logger = logging.getLogger(__name__)

_VECTOR_IMAGE_EXTENSIONS = (".emf", ".wmf")
_VECTOR_PREVIEW_SUFFIX = ".preview.png"

_TEXT_CONTENT_TAGS = (
    '{*}t',
    '{*}tab',
    '{*}br',
    '{*}cr',
    '{*}noBreakHyphen',
)


def parse_spec(doc_path, spec_number: str = None, version: str = None) -> dict:
    """
    Parse a 3GPP spec .doc or .docx into a structured document.
    Accepts optional spec_number and version from the caller as a fallback
    (needed when document metadata is lost, e.g. after LibreOffice conversion).

    Returns:
    {
        "title": "System architecture for the 5G System (5GS)",
        "spec_number": "23.501",
        "version": "18.0.0",
        "release": 18,
        "clauses": [
            {
                "id": "4",
                "title": "Architecture model and concepts",
                "level": 1,
                "body": "text...",
                "children": [...]
            }
        ]
    }
    """
    paths = [Path(path) for path in doc_path] if isinstance(doc_path, (list, tuple)) else [Path(doc_path)]
    if not paths:
        raise ValueError("At least one document path is required")

    documents = [
        _parse_document(
            path,
            spec_number,
            version,
            image_prefix=f"part{index + 1:02d}_" if len(paths) > 1 else "",
        )
        for index, path in enumerate(paths)
    ]
    if len(documents) == 1:
        return documents[0]

    clauses = []
    for document in documents:
        clauses.extend(document.get("clauses", []))

    generic_title = f"3GPP TS {spec_number} v" if spec_number else "3GPP TS "
    title = next(
        (
            document.get("title", "")
            for document in documents
            if document.get("title") and not document["title"].startswith(generic_title)
        ),
        documents[0].get("title", ""),
    )
    return {
        "title": title,
        "spec_number": next((doc.get("spec_number") for doc in documents if doc.get("spec_number")), spec_number or ""),
        "version": next((doc.get("version") for doc in documents if doc.get("version")), version or ""),
        "release": next((doc.get("release") for doc in documents if doc.get("release")), 0),
        "clauses": clauses,
    }


def _parse_document(
    doc_path: Path,
    spec_number: str = None,
    version: str = None,
    image_prefix: str = "",
) -> dict:
    """Parse one physical document part."""
    ext = doc_path.suffix.lower()

    if ext == ".docx":
        return _parse_docx(doc_path, spec_number, version, image_prefix=image_prefix)
    elif ext == ".doc":
        return _parse_doc_via_libreoffice(
            doc_path, spec_number, version, image_prefix=image_prefix
        )
    else:
        raise ValueError(f"Unsupported format: {ext}")


def convert_doc_to_docx(doc_path: Path) -> Path:
    """Convert .doc to .docx using LibreOffice headless."""
    # LibreOffice can't overwrite the same file, so use a temp dir. Give each
    # conversion its own user profile too: concurrent headless processes can
    # otherwise contend for the default profile lock and silently produce no
    # output.
    with tempfile.TemporaryDirectory() as tmpdir:
        temp_root = Path(tmpdir)
        last_error = "unknown error"
        for attempt in range(2):
            attempt_dir = temp_root / f"attempt-{attempt + 1}"
            attempt_dir.mkdir()
            profile_uri = (attempt_dir / "libreoffice-profile").resolve().as_uri()
            try:
                result = subprocess.run(
                    [
                        "libreoffice",
                        f"-env:UserInstallation={profile_uri}",
                        "--headless",
                        "--convert-to",
                        "docx",
                        "--outdir",
                        str(attempt_dir),
                        str(doc_path),
                    ],
                    capture_output=True,
                    text=True,
                    timeout=120,
                )
                if result.returncode != 0:
                    last_error = result.stderr[:500] or f"exit status {result.returncode}"
                    continue
                outputs = list(attempt_dir.glob("*.docx"))
                if not outputs or outputs[0].stat().st_size == 0:
                    last_error = "LibreOffice produced no usable output"
                    continue

                out_path = doc_path.with_suffix(".docx")
                import shutil

                shutil.move(str(outputs[0]), str(out_path))
                return out_path
            except subprocess.TimeoutExpired:
                last_error = "conversion timed out after 120 seconds"

    raise RuntimeError(f"LibreOffice conversion failed after 2 attempts: {last_error}")


def _parse_doc_via_libreoffice(
    doc_path: Path,
    spec_number: str = None,
    version: str = None,
    image_prefix: str = "",
) -> dict:
    """Parse old .doc by converting to .docx first, then parsing."""
    docx_path = convert_doc_to_docx(doc_path)
    return _parse_docx(docx_path, spec_number, version, image_prefix=image_prefix)


def _parse_docx(
    docx_path: Path,
    spec_number: str = None,
    version: str = None,
    image_prefix: str = "",
) -> dict:
    """Parse a .docx file into structured document with image references."""
    from docx import Document

    doc = Document(str(docx_path))

    # Walking python-docx Paragraph.style for every paragraph is very costly on
    # large 3GPP documents. Both metadata and clause extraction use the already
    # loaded XML tree directly instead.
    metadata = _extract_metadata(doc, spec_number, version)
    clauses, para_clause_map, body_elem = _build_clause_tree(doc)

    # Extract images and merge into clause tree
    if para_clause_map:
        cache_dir = Path("cache") / "images" / (metadata.get("spec_number", "_")) / (metadata.get("version", "_"))
        clause_images = _extract_images_from_docx(
            docx_path,
            cache_dir,
            para_clause_map,
            body_elem,
            image_prefix=image_prefix,
        )
        # Also removes temporary source-position keys when there are no images.
        _merge_images(clauses, clause_images)

    return {
        "title": metadata.get("title", ""),
        "spec_number": metadata.get("spec_number", ""),
        "version": metadata.get("version", ""),
        "release": metadata.get("release", 0),
        "clauses": clauses,
    }


def _extract_metadata(doc, spec_number: str = None, version: str = None, _para_data: list = None) -> dict:
    """Extract spec number, version, title from the document."""
    meta = {
        "title": "",
        "spec_number": spec_number or "",
        "version": version or "",
        "release": 0,
    }
    if version:
        major = version.split(".")[0]
        try:
            meta["release"] = int(major)
        except ValueError:
            pass

    # Try core_properties.subject for the canonical title (reliable for native .docx)
    try:
        props = doc.core_properties
        if props.subject:
            meta["title"] = props.subject
    except Exception:
        pass

    # Search the document XML directly. Resolving every python-docx paragraph
    # style is O(paragraphs * styles) in practice and dominated cold parse time.
    if _para_data is not None:
        texts = (text for _style, text in _para_data)
    else:
        texts = (
            _element_text(elem).strip()
            for elem in doc.element.body
            if _local_name(elem) == "p"
        )
    for t in texts:
        if not t:
            continue
        m = re.search(r"3GPP\s+TS\s+(\d+\.\d+)\s+V(\d+)\.(\d+)\.(\d+)", t)
        if m:
            # The archive filename/caller is authoritative. Split 3GPP files
            # occasionally retain an older skeleton version in one part.
            if not spec_number:
                meta["spec_number"] = m.group(1)
            if not version:
                meta["version"] = f"{m.group(2)}.{m.group(3)}.{m.group(4)}"
                meta["release"] = int(m.group(2))
            break

    if meta["title"] and meta["release"]:
        meta["title"] = re.sub(
            r"\bRelease\s+\d+\b",
            f"Release {meta['release']}",
            meta["title"],
            flags=re.IGNORECASE,
        )

    if not meta["title"]:
        if meta["spec_number"] and meta["version"]:
            meta["title"] = f"3GPP TS {meta['spec_number']} v{meta['version']} (Release {meta['release']})"

    return meta


def _build_clause_tree(doc, _para_data: list = None) -> tuple:
    """Build hierarchical clause tree from Word heading styles.
    
    Iterates through ALL body elements (paragraphs + tables) in document order.
    Tables are converted to text and included in clause body.
    
    Returns:
        tuple: (clauses, para_clause_map)
            clauses: list of clause tree nodes
            para_clause_map: dict mapping element index -> unique clause key and ID
    """
    NS = {'w': 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'}
    
    body = doc.element.body
    heading_style_ids, direct_heading_style_ids = _heading_style_ids(doc)
    elements = list(body)
    n = len(elements)
    
    para_clause_map = {}
    entries = []
    
    elem_idx = 0
    while elem_idx < n:
        elem = elements[elem_idx]
        tag = _local_name(elem)
        
        if tag == 'p':
            style_el = elem.find('.//w:pStyle', NS)
            style = style_el.get('{http://schemas.openxmlformats.org/wordprocessingml/2006/main}val', '') if style_el is not None else ''
            text = _element_text(elem).strip()
            folded_style = style.casefold()
            has_heading_outline = _has_heading_outline(elem, NS)
            parsed_clause_id = (
                _extract_clause_id(text)
                if text and (folded_style in heading_style_ids or has_heading_outline)
                else None
            )
            is_heading = text and (
                folded_style in direct_heading_style_ids
                or (
                    parsed_clause_id is not None
                    and (folded_style in heading_style_ids or has_heading_outline)
                )
            )

            if is_heading and text:
                tmp = parsed_clause_id
                clause_id = tmp if tmp else (text.split("\t")[0].strip() if "\t" in text else text)
                level = _clause_level(clause_id, style)
                
                # Use the source heading position as the association key. A
                # clause ID is not globally unique in malformed real-world
                # specs, so using it here can attach a figure to another node.
                clause_key = elem_idx
                association = (clause_key, clause_id)
                para_clause_map[elem_idx] = association
                
                body_parts = []
                elem_idx += 1
                while elem_idx < n:
                    next_elem = elements[elem_idx]
                    next_tag = _local_name(next_elem)
                    
                    if next_tag == 'p':
                        next_style_el = next_elem.find('.//w:pStyle', NS)
                        next_style = next_style_el.get('{http://schemas.openxmlformats.org/wordprocessingml/2006/main}val', '') if next_style_el is not None else ''
                        next_text = _element_text(next_elem).strip()
                        folded_next_style = next_style.casefold()
                        next_has_heading_outline = _has_heading_outline(next_elem, NS)
                        next_clause_id = (
                            _extract_clause_id(next_text)
                            if next_text and (
                                folded_next_style in heading_style_ids
                                or next_has_heading_outline
                            )
                            else None
                        )
                        next_is_heading = next_text and (
                            folded_next_style in direct_heading_style_ids
                            or (
                                next_clause_id is not None
                                and (
                                    folded_next_style in heading_style_ids
                                    or next_has_heading_outline
                                )
                            )
                        )

                        if next_is_heading and next_text:
                            break
                        if not next_style.startswith("toc"):
                            para_clause_map[elem_idx] = association
                        if next_text and not next_style.startswith("toc"):
                            body_parts.append(next_text)
                    elif next_tag == 'tbl':
                        para_clause_map[elem_idx] = association
                        table_text = _table_to_text(next_elem, NS)
                        if table_text:
                            body_parts.append(table_text)
                    
                    elem_idx += 1
                
                entries.append((level, text, body_parts, clause_key))
            else:
                elem_idx += 1
        elif tag == 'tbl':
            elem_idx += 1
        else:
            elem_idx += 1
    
    root = _build_tree(entries)
    return root, para_clause_map, body


def _has_heading_outline(element, nsmap) -> bool:
    """Return whether a paragraph explicitly participates in Word's outline."""
    outline = element.find('./w:pPr/w:outlineLvl', nsmap)
    if outline is None:
        return False
    value = outline.get(
        '{http://schemas.openxmlformats.org/wordprocessingml/2006/main}val',
        '9',
    )
    try:
        return 0 <= int(value) < 9
    except ValueError:
        return False


def _heading_style_ids(doc) -> tuple[set[str], set[str]]:
    """Find built-in and derived paragraph styles used for clause headings.

    Some 3GPP templates introduce short custom IDs such as ``H6`` which are
    based on a built-in Heading style. Looking only for the word "heading" in
    the paragraph's immediate style ID silently folds those clauses into their
    parent's body.
    """
    NS = {'w': 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'}
    attr = '{http://schemas.openxmlformats.org/wordprocessingml/2006/main}'
    based_on = {}
    direct_heading_ids = set()
    for style in doc.styles.element.findall('./w:style', NS):
        if style.get(f'{attr}type', 'paragraph') != 'paragraph':
            continue
        style_id = style.get(f'{attr}styleId', '')
        if not style_id:
            continue
        folded_id = style_id.casefold()
        name = style.find('./w:name', NS)
        style_name = name.get(f'{attr}val', '') if name is not None else ''
        base = style.find('./w:basedOn', NS)
        base_id = base.get(f'{attr}val', '') if base is not None else ''
        based_on[folded_id] = base_id.casefold()
        if 'heading' in folded_id or 'heading' in style_name.casefold():
            direct_heading_ids.add(folded_id)

    heading_ids = set(direct_heading_ids)
    changed = True
    while changed:
        changed = False
        for style_id, base_id in based_on.items():
            if style_id not in heading_ids and base_id in heading_ids:
                heading_ids.add(style_id)
                changed = True
    return heading_ids, direct_heading_ids


def _local_name(element) -> str:
    """Return an XML element's local tag name without allocating XPath state."""
    return element.tag.rsplit('}', 1)[-1]


def _element_text(element) -> str:
    """Extract WordprocessingML text without adding spaces between text runs.

    Run boundaries are formatting metadata, not word boundaries. Adding spaces
    at each boundary corrupts acronyms and creates false diffs. Tabs and line
    breaks are the only run-level elements that add separators.
    """
    parts = []
    # lxml filters by tag in C, avoiding a Python iteration over the many
    # formatting/property elements that never contribute visible text.
    for descendant in element.iter(*_TEXT_CONTENT_TAGS):
        name = descendant.tag.rsplit('}', 1)[-1]
        if name == 't' and descendant.text:
            parts.append(descendant.text)
        elif name == 'tab':
            parts.append('\t')
        elif name in ('br', 'cr'):
            parts.append('\n')
        elif name == 'noBreakHyphen':
            parts.append('-')
    return ''.join(parts)


def _table_to_text(table_elem, nsmap) -> str:
    """Convert a Word table XML element to formatted text.

    Returns a pipe-delimited table representation suitable for diff display.
    Cell paragraphs are flattened so a visual row always remains one logical
    line. Horizontal spans get empty placeholder cells, allowing the browser
    to reconstruct a stable rectangular table.
    """
    rows = table_elem.findall('.//w:tr', nsmap)
    if not rows:
        return ''
    
    table_rows = []
    for row in rows:
        cells = row.findall('./w:tc', nsmap)
        if not cells:
            cells = row.findall('./w:sdt/w:sdtContent/w:tc', nsmap)
        cell_texts = []
        for cell in cells:
            paragraphs = cell.findall('.//w:p', nsmap)
            text = '\n'.join(
                part for part in (
                    ' '.join(_element_text(p).split()) for p in paragraphs
                )
                if part
            )
            # Backslash escaping keeps literal pipes from being interpreted as
            # additional columns by the lightweight browser renderer. Encoded
            # newlines preserve paragraph structure without splitting a row.
            text = (
                text.replace('\\', '\\\\')
                .replace('\n', '\\n')
                .replace('|', '\\|')
            )
            cell_texts.append(text)
            grid_span = cell.find('./w:tcPr/w:gridSpan', nsmap)
            if grid_span is not None:
                value = grid_span.get(
                    '{http://schemas.openxmlformats.org/wordprocessingml/2006/main}val',
                    '1',
                )
                try:
                    cell_texts.extend([''] * max(0, int(value) - 1))
                except ValueError:
                    pass
        table_rows.append(cell_texts)
    
    if not table_rows:
        return ''
    
    max_cols = max(len(r) for r in table_rows)
    
    for row in table_rows:
        while len(row) < max_cols:
            row.append('')
    
    lines = []
    for row in table_rows:
        lines.append('| ' + ' | '.join(row) + ' |')
    
    return '\n'.join(lines)


def _heading_level(style_name: str) -> int:
    """Extract heading level from Word style name.
    Accepts display names (``Heading 2``) and XML style IDs (``Heading2``).
    Default: 1
    """
    match = re.search(r"heading\s*(\d+)", style_name, re.IGNORECASE)
    return int(match.group(1)) if match else 1


def _clause_level(clause_id: str, style_name: str) -> int:
    """Derive hierarchy from the stable identifier, falling back to style.

    3GPP templates use ``Heading8`` for an annex heading and restart at
    ``Heading1`` for ``A.1``. The raw Word style therefore cannot represent
    document hierarchy across the main body and annexes.
    """
    if clause_id.casefold().startswith("annex "):
        return 1
    if re.fullmatch(r"(?:\d+[A-Za-z]*|[A-Za-z]+)(?:\.\d+[A-Za-z]*)*", clause_id):
        return clause_id.count(".") + 1
    return _heading_level(style_name)


def _extract_images_from_docx(
    docx_path,
    cache_dir,
    para_clause_map,
    body_elem,
    image_prefix="",
):
    """Extract images from .docx and associate with clauses by element index.

    Args:
        body_elem: Pre-parsed lxml body element from _build_clause_tree.
        para_clause_map: Mapping from element index to clause_id.

    Returns dict mapping clause_id -> list of image info dicts.
    """
    import zipfile
    from lxml import etree

    NS = {
        'w': 'http://schemas.openxmlformats.org/wordprocessingml/2006/main',
        'r': 'http://schemas.openxmlformats.org/officeDocument/2006/relationships',
        'v': 'urn:schemas-microsoft-com:vml',
        'a': 'http://schemas.openxmlformats.org/drawingml/2006/main',
    }
    R_ATTR = '{http://schemas.openxmlformats.org/officeDocument/2006/relationships}'

    clause_images = {}

    try:
        with zipfile.ZipFile(str(docx_path), 'r') as z:
            rels_xml = etree.parse(z.open('word/_rels/document.xml.rels'))
            rel_map = {}
            for rel in rels_xml.xpath('//*[local-name()="Relationship"]'):
                rel_id = rel.get('Id')
                target = rel.get('Target')
                rel_type = rel.get('Type', '')
                if 'relationships/image' in rel_type:
                    rel_map[rel_id] = target

            if not rel_map:
                return clause_images

            # Reuse the already-parsed body element instead of re-parsing document.xml
            elements = list(body_elem)

            for elem_idx, elem in enumerate(elements):
                if elem_idx not in para_clause_map:
                    continue

                association = para_clause_map[elem_idx]
                if isinstance(association, tuple):
                    clause_key, clause_id = association
                else:
                    # Backwards-compatible with callers that still provide a
                    # simple ID mapping.
                    clause_key = clause_id = association
                seen_targets = set()

                for imagedata in elem.iter('{urn:schemas-microsoft-com:vml}imagedata'):
                    rel_id = imagedata.get(f'{R_ATTR}id')
                    if rel_id and rel_id in rel_map:
                        target = rel_map[rel_id]
                        if target not in seen_targets:
                            seen_targets.add(target)
                            info = _save_image(
                                z, target, cache_dir, clause_id, image_prefix=image_prefix
                            )
                            if info:
                                clause_images.setdefault(clause_key, []).append(info)

                for blip in elem.iter('{http://schemas.openxmlformats.org/drawingml/2006/main}blip'):
                    embed = blip.get(f'{R_ATTR}embed')
                    if embed and embed in rel_map:
                        target = rel_map[embed]
                        if target not in seen_targets:
                            seen_targets.add(target)
                            info = _save_image(
                                z, target, cache_dir, clause_id, image_prefix=image_prefix
                            )
                            if info:
                                clause_images.setdefault(clause_key, []).append(info)

    except Exception as e:
        import warnings
        warnings.warn(f"Image extraction failed for {docx_path}: {e}")

    if clause_images:
        _convert_emf_to_png(Path(cache_dir), clause_images)

    return clause_images


def _vector_preview_path(vector_path):
    return vector_path.with_name(f"{vector_path.stem}{_VECTOR_PREVIEW_SUFFIX}")


def _convert_vectors_with_libreoffice(pending):
    """Render metafiles through LibreOffice's graphic engine.

    LibreOffice preserves Office drawing records that Inkscape occasionally
    misinterprets (for example, missing punctuation in EMF text).  pyuno runs
    in a helper process so native conversion failures cannot crash a web
    worker.
    """
    helper = Path(__file__).with_name("libreoffice_image_converter.py")
    command = [sys.executable, str(helper)]
    for source in pending:
        command.extend((str(source), str(_vector_preview_path(source))))
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=min(600, max(120, len(pending) * 8)),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.warning("LibreOffice image preview conversion failed: %s", exc)
        return
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "unknown error").strip()
        logger.warning(
            "LibreOffice image preview conversion was incomplete: %s",
            detail[:1000],
        )


def _is_emf_content(path):
    """Identify EMF data even when LibreOffice gave it a .wmf extension."""
    try:
        with path.open("rb") as source:
            header = source.read(44)
    except OSError:
        return False
    return (
        len(header) >= 44
        and header[:4] == b"\x01\x00\x00\x00"
        and header[40:44] == b" EMF"
    )


def _promote_preview(source, destination):
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(
        f".{destination.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
    )
    try:
        shutil.copyfile(source, temporary)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def _convert_vectors_with_inkscape(pending):
    """High-resolution fallback for systems without a working UNO bridge."""
    with tempfile.TemporaryDirectory(prefix="3gpp-inkscape-images-") as tempdir:
        temp_root = Path(tempdir)
        aliases = {}
        for source in pending:
            suffix = ".emf" if _is_emf_content(source) else source.suffix.lower()
            alias = temp_root / f"{source.stem}.preview{suffix}"
            shutil.copyfile(source, alias)
            aliases[alias] = _vector_preview_path(source)

        try:
            result = subprocess.run(
                [
                    "inkscape",
                    "--export-type=png",
                    "--export-dpi=300",
                    *(str(path) for path in aliases),
                ],
                capture_output=True,
                text=True,
                timeout=min(600, max(120, len(aliases) * 10)),
            )
            if result.returncode != 0:
                logger.warning(
                    "Inkscape image preview batch failed: %s",
                    str(result.stderr or result.stdout or result.returncode)[:1000],
                )
        except (OSError, subprocess.TimeoutExpired) as exc:
            logger.warning("Inkscape image preview conversion failed: %s", exc)
            return

        # A malformed vector may make a batch incomplete. Retry only missing
        # outputs so one bad figure does not hide every other image.
        for alias, destination in aliases.items():
            rendered = alias.with_suffix(".png")
            if not rendered.exists():
                try:
                    subprocess.run(
                        [
                            "inkscape",
                            str(alias),
                            "--export-dpi=300",
                            "--export-filename",
                            str(rendered),
                        ],
                        capture_output=True,
                        text=True,
                        timeout=90,
                    )
                except (OSError, subprocess.TimeoutExpired):
                    continue
            if rendered.exists() and rendered.stat().st_size:
                _promote_preview(rendered, destination)


def _convert_emf_to_png(cache_dir, clause_images):
    """Create faithful browser previews while retaining original vectors."""
    vector_files = sorted(
        path
        for path in Path(cache_dir).iterdir()
        if path.is_file() and path.suffix.lower() in _VECTOR_IMAGE_EXTENSIONS
    )
    if vector_files:
        pending = [
            path for path in vector_files if not _vector_preview_path(path).exists()
        ]
        if pending:
            _convert_vectors_with_libreoffice(pending)
            missing = [
                path for path in pending if not _vector_preview_path(path).exists()
            ]
            if missing:
                _convert_vectors_with_inkscape(missing)

    for images in clause_images.values():
        for img in images:
            src = img["src"]
            if src.lower().endswith(_VECTOR_IMAGE_EXTENSIONS):
                original_name = Path(src).name
                preview_path = _vector_preview_path(Path(cache_dir) / original_name)
                img["original_src"] = original_name
                if preview_path.exists():
                    img["src"] = preview_path.name


def _save_image(zip_file, target, cache_dir, clause_id, image_prefix=""):
    """Extract image from ZIP to cache and return image info dict."""
    try:
        img_path = f'word/{target}'
        img_data = zip_file.read(img_path)
        img_filename = f"{image_prefix}{target.split('/')[-1]}"

        cache_dir = Path(cache_dir)
        cache_dir.mkdir(parents=True, exist_ok=True)
        cache_path = cache_dir / img_filename
        if not cache_path.exists():
            cache_path.write_bytes(img_data)

        src = img_filename
        image_info = {
            'id': img_filename,
            'src': src,
            'alt': f'Figure in clause {clause_id}',
            # Filenames are generated independently in every release. Content
            # identity lets the diff engine distinguish a renamed image from a
            # genuinely changed technical figure.
            'sha256': hashlib.sha256(img_data).hexdigest(),
        }
        if img_filename.lower().endswith(_VECTOR_IMAGE_EXTENSIONS):
            preview_path = _vector_preview_path(cache_path)
            image_info['original_src'] = img_filename
            if preview_path.exists():
                image_info['src'] = preview_path.name

        return image_info
    except Exception:
        return None


def _merge_images(clauses, clause_images):
    """Recursively merge image references into clause tree nodes."""
    for node in clauses:
        source_key = node.pop("_source_key", None)
        if source_key in clause_images:
            node["images"] = clause_images[source_key]
        _merge_images(node.get("children", []), clause_images)


def _build_tree(entries: list, base_level: int = 1) -> list:
    """Convert flat list of (level, heading, body) into nested tree.
    Only includes entries at level >= base_level.
    """
    tree = []
    stack = []  # stack of parent nodes
    # Word heading order is occasionally inconsistent with the clause
    # identifiers.  For example, TS 29.522 v19 places ``5.6.0
    # Introduction`` between ``5.6.1 Resources`` and ``5.6.1.1 Overview``.
    # Keep earlier nodes addressable so an explicit numeric prefix wins over
    # the merely most-recent heading at the same depth.
    nodes_by_id = defaultdict(list)
    parent_by_identity = {}

    for entry in entries:
        level, heading, body = entry[:3]
        source_key = entry[3] if len(entry) > 3 else None
        # Skip entries that don't have clause-style IDs (Foreword, etc.)
        clause_id = _extract_clause_id(heading)

        node = {
            "id": clause_id or heading.split("\t")[0].strip(),
            "title": heading.split("\t")[-1].strip() if "\t" in heading else heading,
            "raw_heading": heading,
            "level": level,
            "body": "\n".join(body),
            "changed": False,
            "children": [],
        }
        if source_key is not None:
            node["_source_key"] = source_key

        parent = _numbered_parent(
            node["id"],
            nodes_by_id,
            stack,
            parent_by_identity,
        )
        if parent is None:
            # Fall back to Word's/document-order hierarchy when the expected
            # numbered parent is absent (or the heading has a malformed ID).
            while stack and stack[-1]["level"] >= level:
                stack.pop()
            parent = stack[-1] if stack else None

        if parent is not None:
            parent["children"].append(node)
        else:
            tree.append(node)

        parent_by_identity[id(node)] = parent
        nodes_by_id[node["id"].casefold()].append(node)

        # Rebuild the active ancestry from the actual attachment. This keeps
        # the fallback correct after jumping back to an earlier numbered
        # parent rather than leaving an out-of-order sibling on the stack.
        ancestry = []
        ancestor = parent
        while ancestor is not None:
            ancestry.append(ancestor)
            ancestor = parent_by_identity.get(id(ancestor))
        stack = list(reversed(ancestry)) + [node]

    return tree


def _numbered_parent(
    clause_id: str,
    nodes_by_id: dict,
    stack: list,
    parent_by_identity: dict,
):
    """Return a nearby preceding node named by a clause ID's prefix.

    The locality check matters for source typos. A heading numbered
    ``5.4.2...`` inside clause 5.20 must not jump hundreds of paragraphs back
    to the real clause 5.4 merely because that prefix exists somewhere in the
    document.
    """
    if not clause_id or clause_id.casefold().startswith("annex "):
        return None
    if "." not in clause_id:
        return None

    parent_id = clause_id.rsplit(".", 1)[0]
    candidate_ids = [parent_id]
    # Annex subclauses are written as A.1 while their parent is labelled
    # "Annex A" rather than simply "A".
    if re.fullmatch(r"[A-Za-z]+", parent_id):
        candidate_ids.insert(0, f"Annex {parent_id}")
    active_ancestors = {id(node) for node in stack}
    for candidate_id in candidate_ids:
        candidates = nodes_by_id.get(candidate_id.casefold())
        if candidates:
            candidate = candidates[-1]
            candidate_parent = parent_by_identity.get(id(candidate))
            if (
                id(candidate) in active_ancestors
                or (
                    candidate_parent is not None
                    and id(candidate_parent) in active_ancestors
                )
            ):
                return candidate
    return None


def _extract_clause_id(heading: str) -> str:
    """Extract clause number from heading text.
    '4.2.3\tNon-roaming reference architecture' -> '4.2.3'
    '4.2.3 Non-roaming reference architecture' -> '4.2.3'
    'D.5 Support for overlay networks' -> 'D.5'
    'Annex D (informative): deployment options' -> 'Annex D'
    Returns None if no clause number found.
    """
    # Annex headings need a stable identifier even when their descriptive
    # title changes between releases.
    annex_match = re.match(r"^\s*Annex\s+([A-Za-z]+)\b", heading, re.IGNORECASE)
    if annex_match:
        return f"Annex {annex_match.group(1).upper()}"

    # 3GPP sources sometimes insert spaces around dots ("5.2. 1" or
    # "4 . 3"). Annex subclauses use an alphabetic first segment ("D.5").
    # Accept optional letter suffixes as used by identifiers such as 5.35A.1.
    clause_match = re.match(
        r"^\s*("
        r"(?:\d+[A-Za-z]?(?:\s*\.\s*\d+[A-Za-z]?)*)"
        r"|(?:[A-Za-z]+(?:\s*\.\s*\d+[A-Za-z]?)+)"
        r")(?=\s|$)",
        heading,
    )
    if clause_match:
        return re.sub(r"\s*\.\s*", ".", clause_match.group(1))

    # A handful of source paragraphs accidentally retain normal body text in
    # front of the actual numbered heading, while still separating the title
    # with a tab (for example ``...specification.5.27.4\tNotifications`` in
    # TS 29.522 v18). In that constrained shape, recover the final dotted
    # number rather than using the entire polluted prefix as the clause ID.
    if "\t" in heading:
        prefix = heading.split("\t", 1)[0].rstrip()
        trailing_match = re.search(
            r"(?<![A-Za-z0-9])"
            r"(\d+[A-Za-z]?(?:\s*\.\s*\d+[A-Za-z]?)+)\s*$",
            prefix,
        )
        if trailing_match:
            return re.sub(r"\s*\.\s*", ".", trailing_match.group(1))

    return None


def clause_count(clauses: list) -> int:
    """Count total clauses (recursive)."""
    count = 0
    for c in clauses:
        count += 1
        if c.get("children"):
            count += clause_count(c["children"])
    return count


if __name__ == "__main__":
    import json
    import sys

    path = Path(sys.argv[1])
    doc = parse_spec(path)
    print(f"Title: {doc['title']}")
    print(f"Spec: {doc['spec_number']} v{doc['version']}")
    print(f"Total clauses: {clause_count(doc['clauses'])}")

    # Print top-level clauses
    for c in doc["clauses"]:
        n = clause_count([c])
        print(f"  [{c['id']}] {c['title']} ({n} clauses, {len(c.get('body',''))} chars)")
