"""
Clause-by-clause diff engine for 3GPP specifications.
Compares two structured clause trees and produces a diff tree.
"""
import difflib
import re
from collections import Counter, defaultdict


def diff_trees(
    old_clauses: list,
    new_clauses: list,
    *,
    _old_parent_id: str = "",
    _new_parent_id: str = "",
    _context: dict = None,
) -> list:
    """
    Compare two clause trees and produce a diff tree.
    Each node gets a status: 'unchanged' | 'modified' | 'added' | 'deleted'.

    Matching:
      1. High-confidence renumbering anchors (subject/body/relative suffix)
      2. By clause ID (e.g. "4.2.3")
      3. Unmatched old  -> deleted
      4. Unmatched new  -> added

    Ordering follows the new document. Deleted clauses are inserted before
    their next surviving old-document sibling so they remain near their
    original position.

    This function only processes top-level nodes; recursion handles children.
    """
    if _context is None:
        _context = _build_tree_match_context(old_clauses, new_clauses)

    global_matches = _context["matches"]
    global_reserved_old = _context["reserved_old"]
    global_reserved_new = _context["reserved_new"]
    old_by_id = _build_id_map(old_clauses)
    new_id_counts = Counter(node.get("id", "") for node in new_clauses)

    # Reserve high-confidence semantic pairs before matching identical IDs.
    # This matters when inserting a clause shifts every following number: an
    # ID-first pass would otherwise pair old 5.1 (Feature A) with new 5.1 (the
    # insertion), then report Feature A at 5.2 as a separate addition.
    renumbered_matches = _match_renumbered_siblings(
        old_clauses,
        new_clauses,
        _old_parent_id,
        _new_parent_id,
        blocked_old_ids=global_reserved_old,
        blocked_new_ids=global_reserved_new,
    )
    reserved_old_ids = {id(node) for node in renumbered_matches.values()}

    result = []
    processed_old_ids = reserved_old_ids | {
        id(node) for node in old_clauses if id(node) in global_reserved_old
    }
    matched_results = {}

    # Process new clauses in document order, matching against old
    for new_node in new_clauses:
        nid = new_node["id"]

        old_node = global_matches.get(id(new_node))
        if old_node is None:
            old_node = renumbered_matches.get(id(new_node))
        if old_node is None:
            candidates = old_by_id.get(nid, [])
            old_node = _select_old_node(
                candidates,
                new_node,
                processed_old_ids,
                require_title_similarity=(
                    len(candidates) > 1
                    or new_id_counts[nid] > 1
                    # Annex letters are frequently reused for unrelated material
                    # when new annexes are inserted between releases.
                    or nid.casefold().startswith("annex ")
                ),
            )
        if old_node is None and nid.casefold().startswith("annex "):
            old_node = _select_renamed_annex(old_clauses, new_node, processed_old_ids)

        if old_node is not None:
            processed_old_ids.add(id(old_node))
            children = diff_trees(
                old_node.get("children", []),
                new_node.get("children", []),
                _old_parent_id=old_node.get("id", ""),
                _new_parent_id=new_node.get("id", ""),
                _context=_context,
            )
            body_moved_to_child = _promote_parent_body_move(
                old_node, new_node, children
            )
            comparison_old_node = old_node
            if body_moved_to_child:
                # The old parent became a container. Its former content is
                # compared at the new child, so it must not also appear here
                # as a complete deletion.
                comparison_old_node = {
                    **old_node,
                    "body": new_node.get("body", ""),
                    "images": new_node.get("images", []),
                }
            identifier_changed = old_node.get("id") != nid
            if identifier_changed or _is_modified(comparison_old_node, new_node):
                result_node = {
                    "id": nid,
                    "title": new_node["title"],
                    "old_title": old_node["title"],
                    "level": new_node["level"],
                    "status": "modified",
                    "old_body": comparison_old_node.get("body", ""),
                    "new_body": new_node.get("body", ""),
                    "old_images": comparison_old_node.get("images", []),
                    "new_images": new_node.get("images", []),
                    "children": children,
                }
                if identifier_changed:
                    result_node["old_id"] = old_node.get("id", "")
            else:
                result_node = {
                    "id": nid,
                    "title": new_node["title"],
                    "level": new_node["level"],
                    "status": "unchanged",
                    "body": new_node.get("body", ""),
                    "images": new_node.get("images", []),
                    "children": children,
                }
            matched_results[id(old_node)] = result_node
        else:
            # New clause not in old -> added
            result_node = {
                "id": nid,
                "title": new_node["title"],
                "level": new_node["level"],
                "status": "added",
                "body": new_node.get("body", ""),
                "images": new_node.get("images", []),
                "children": diff_trees(
                    [],
                    new_node.get("children", []),
                    _new_parent_id=new_node.get("id", ""),
                    _context=_context,
                ),
            }
        result.append(result_node)

    # Group deleted clauses by their next surviving old-document sibling.
    # This preserves source order without sorting alphabetic annex IDs ahead
    # of numbered clauses.
    deleted_before = defaultdict(list)
    pending_deleted = []
    for old_node in old_clauses:
        if id(old_node) not in processed_old_ids:
            pending_deleted.append({
                "id": old_node["id"],
                "title": old_node["title"],
                "level": old_node["level"],
                "status": "deleted",
                "body": old_node.get("body", ""),
                "images": old_node.get("images", []),
                "children": diff_trees(
                    old_node.get("children", []),
                    [],
                    _old_parent_id=old_node.get("id", ""),
                    _context=_context,
                ),
            })
        elif pending_deleted:
            anchor = matched_results.get(id(old_node))
            if anchor is not None:
                deleted_before[id(anchor)].extend(pending_deleted)
                pending_deleted = []

    if not matched_results:
        return pending_deleted + result

    ordered_result = []
    for node in result:
        ordered_result.extend(deleted_before.get(id(node), ()))
        ordered_result.append(node)
    ordered_result.extend(pending_deleted)
    return ordered_result


def _promote_parent_body_move(old_node: dict, new_node: dict, child_results: list) -> bool:
    """Recognize content moved from a leaf into one new direct child.

    A release sometimes turns an existing clause into a container and moves
    its nearly unchanged body into a newly numbered child (for example,
    ``5`` -> ``5.1``). Ordinary ID matching correctly pairs the two parent
    nodes, but would then render the whole old body as deleted and the child as
    added. Only promote a unique, high-confidence candidate so a genuine split
    into several new clauses remains an addition/deletion.
    """
    if old_node.get("id") != new_node.get("id"):
        return False
    if old_node.get("children") or not new_node.get("children"):
        return False

    old_body = _normalize_whitespace(old_node.get("body", ""))
    new_parent_body = _normalize_whitespace(new_node.get("body", ""))
    if not 120 <= len(old_body) <= 50_000:
        return False
    if len(new_parent_body) > max(80, len(old_body) // 10):
        return False

    result_by_identity = defaultdict(list)
    for result in child_results:
        if result.get("status") == "added":
            result_by_identity[(result.get("id"), result.get("title"))].append(result)

    candidates = []
    old_title = _title_for_matching(old_node)
    for new_child in new_node.get("children", []):
        matches = result_by_identity.get(
            (new_child.get("id"), new_child.get("title")), ()
        )
        if len(matches) != 1:
            continue
        new_body = _normalize_whitespace(new_child.get("body", ""))
        if not 120 <= len(new_body) <= 50_000:
            continue
        if min(len(old_body), len(new_body)) / max(len(old_body), len(new_body)) < 0.7:
            continue
        title_score = difflib.SequenceMatcher(
            None, old_title, _title_for_matching(new_child)
        ).ratio()
        if title_score < 0.55:
            continue
        body_score = difflib.SequenceMatcher(None, old_body, new_body).ratio()
        if body_score < 0.88:
            continue
        candidates.append((new_child, matches[0]))

    if len(candidates) != 1:
        return False

    new_child, result = candidates[0]
    result.update({
        "old_title": old_node.get("title", ""),
        "status": "modified",
        "change_type": "moved",
        "old_id": old_node.get("id", ""),
        "old_body": old_node.get("body", ""),
        "new_body": new_child.get("body", ""),
        "old_images": old_node.get("images", []),
        "new_images": new_child.get("images", []),
    })
    result.pop("body", None)
    result.pop("images", None)
    return True


_GENERIC_RENUMBER_TITLES = {
    "general",
    "overview",
    "introduction",
    "scope",
    "references",
    "definitions",
    "resource definition",
    "resource methods",
    "get",
    "post",
    "put",
    "patch",
    "delete",
    "notifications",
    "data model",
    "used features",
    "error handling",
    "protocol errors",
    "application errors",
}


def _build_tree_match_context(old_clauses: list, new_clauses: list) -> dict:
    """Reserve high-confidence matches that moved outside their old parent.

    Recursive sibling matching cannot see a clause promoted to another level
    or moved beneath a different parent. 3GPP documents commonly leave a
    ``(moved)`` stub at the old identifier, so matching that stub by ID would
    otherwise turn the moved subtree into a large deletion plus addition.
    """
    old_records = _tree_records(old_clauses)
    new_records = _tree_records(new_clauses)
    new_by_id = defaultdict(list)
    for record in new_records:
        new_by_id[record["node"].get("id", "")].append(record["node"])

    old_by_title = defaultdict(list)
    new_by_title = defaultdict(list)
    for record in old_records:
        key = _distinctive_title_key(record["node"], "")
        if key is not None:
            old_by_title[key].append(record)
    for record in new_records:
        key = _distinctive_title_key(record["node"], "")
        if key is not None:
            new_by_title[key].append(record)

    matches = {}
    reserved_old = set()
    reserved_new = set()
    for key, old_candidates in old_by_title.items():
        new_candidates = new_by_title.get(key, ())
        if len(old_candidates) != 1 or len(new_candidates) != 1:
            continue
        old_record = old_candidates[0]
        new_record = new_candidates[0]
        old_node = old_record["node"]
        new_node = new_record["node"]
        if old_node.get("id") == new_node.get("id"):
            continue
        if old_record["parent_id"] == new_record["parent_id"]:
            # The cheaper sibling matcher already handles ordinary inserted
            # numbers and carries generic children after the parent is paired.
            continue
        if not _cross_parent_match_evidence(
            old_record, new_record, new_by_id
        ):
            continue
        matches[id(new_node)] = old_node
        reserved_old.add(id(old_node))
        reserved_new.add(id(new_node))

    _pair_voided_cross_parent_moves(
        old_records,
        new_records,
        new_by_id,
        matches,
        reserved_old,
        reserved_new,
    )

    return {
        "matches": matches,
        "reserved_old": reserved_old,
        "reserved_new": reserved_new,
    }


def _pair_voided_cross_parent_moves(
    old_records: list,
    new_records: list,
    new_by_id: dict,
    matches: dict,
    reserved_old: set,
    reserved_new: set,
):
    """Match a renamed subtree whose old identifier became Void/(moved)."""
    proposals = []
    for old_record in old_records:
        old_node = old_record["node"]
        if id(old_node) in reserved_old or not _eligible_renumber_node(old_node):
            continue
        if len(old_node.get("children", ())) < 2:
            continue
        old_title = _title_for_matching(old_node)
        if len(old_title) < 12:
            continue
        old_title_tokens = set(re.findall(r"[a-z0-9]+", old_title))
        stubs = new_by_id.get(old_node.get("id", ""), ())
        if not any(_is_move_or_void_stub(stub) for stub in stubs):
            continue

        candidates = []
        for new_record in new_records:
            new_node = new_record["node"]
            if (
                id(new_node) in reserved_new
                or not _eligible_renumber_node(new_node)
                or new_node.get("id") == old_node.get("id")
                or new_record["parent_id"] == old_record["parent_id"]
                or len(new_node.get("children", ())) < 2
            ):
                continue
            new_title = _title_for_matching(new_node)
            if len(new_title) < 12:
                continue
            new_title_tokens = set(re.findall(r"[a-z0-9]+", new_title))
            common_tokens = old_title_tokens & new_title_tokens
            if (
                len(common_tokens) < 2
                or len(common_tokens)
                / max(1, min(len(old_title_tokens), len(new_title_tokens)))
                < 0.5
            ):
                continue
            title_score = difflib.SequenceMatcher(
                None, old_title, new_title
            ).ratio()
            if title_score < 0.65:
                continue
            child_overlap = _child_subject_overlap(old_node, new_node)
            old_body = _substantial_body_key(old_node, "")
            new_body = _substantial_body_key(new_node, "")
            if child_overlap < 0.2 and not (
                old_body is not None and old_body == new_body
            ):
                continue
            candidates.append((title_score, new_node))

        candidates.sort(key=lambda item: item[0], reverse=True)
        if not candidates:
            continue
        score, best = candidates[0]
        runner_up = candidates[1][0] if len(candidates) > 1 else 0.0
        if score - runner_up >= 0.1:
            proposals.append((old_node, best))

    proposed_new_counts = Counter(id(new_node) for _, new_node in proposals)
    for old_node, new_node in proposals:
        if proposed_new_counts[id(new_node)] != 1:
            continue
        reserved_old.add(id(old_node))
        reserved_new.add(id(new_node))
        matches[id(new_node)] = old_node


def _is_move_or_void_stub(node: dict) -> bool:
    title = _title_for_matching(node)
    text = _normalize_whitespace(
        f"{node.get('title', '')} {node.get('body', '')}"
    ).casefold()
    return title == "void" or "moved" in text


def _tree_records(clauses: list, parent_id: str = "", depth: int = 0) -> list:
    records = []
    for node in clauses:
        records.append({"node": node, "parent_id": parent_id, "depth": depth})
        records.extend(
            _tree_records(node.get("children", []), node.get("id", ""), depth + 1)
        )
    return records


def _cross_parent_match_evidence(old_record: dict, new_record: dict, new_by_id: dict) -> bool:
    old_node = old_record["node"]
    new_node = new_record["node"]
    if old_record["depth"] == new_record["depth"]:
        return True

    old_body = _substantial_body_key(old_node, "")
    new_body = _substantial_body_key(new_node, "")
    if old_body is not None and old_body == new_body:
        return True
    if _child_subject_overlap(old_node, new_node) >= 0.75:
        return True

    target_id = _normalize_whitespace(new_node.get("id", "")).casefold()
    for stub in new_by_id.get(old_node.get("id", ""), ()):
        if stub is new_node:
            continue
        stub_text = _normalize_whitespace(
            f"{stub.get('title', '')} {stub.get('body', '')}"
        ).casefold()
        if target_id and target_id in stub_text and (
            "moved" in stub_text or "described in clause" in stub_text
        ):
            return True
    return False


def _child_subject_overlap(old_node: dict, new_node: dict) -> float:
    old_subjects = Counter(
        _title_for_matching(child) for child in old_node.get("children", [])
    )
    new_subjects = Counter(
        _title_for_matching(child) for child in new_node.get("children", [])
    )
    old_subjects.pop("", None)
    new_subjects.pop("", None)
    total = max(sum(old_subjects.values()), sum(new_subjects.values()))
    if total < 2:
        return 0.0
    common = sum((old_subjects & new_subjects).values())
    return common / total


def _match_renumbered_siblings(
    old_clauses: list,
    new_clauses: list,
    old_parent_id: str,
    new_parent_id: str,
    blocked_old_ids=None,
    blocked_new_ids=None,
) -> dict:
    """Find conservative one-to-one matches whose clause IDs changed.

    Exact, distinctive subjects and substantial unchanged bodies are reliable
    anchors. Once a parent was renumbered, equal relative child suffixes provide
    a final structural anchor for generic headings such as "General".
    """
    matches = {}
    used_old = set(blocked_old_ids or ())
    used_new = set(blocked_new_ids or ())
    old_id_counts = Counter(node.get("id", "") for node in old_clauses)
    new_id_counts = Counter(node.get("id", "") for node in new_clauses)

    def pair_unique(
        key_function,
        old_nodes=None,
        new_nodes=None,
        *,
        require_same_level=True,
    ):
        old_by_key = defaultdict(list)
        new_by_key = defaultdict(list)
        for node in old_clauses if old_nodes is None else old_nodes:
            if id(node) not in used_old:
                key = key_function(node, old_parent_id)
                if key is not None:
                    old_by_key[key].append(node)
        for node in new_clauses if new_nodes is None else new_nodes:
            if id(node) not in used_new:
                key = key_function(node, new_parent_id)
                if key is not None:
                    new_by_key[key].append(node)

        for key, old_nodes in old_by_key.items():
            new_nodes = new_by_key.get(key, ())
            if len(old_nodes) != 1 or len(new_nodes) != 1:
                continue
            old_node = old_nodes[0]
            new_node = new_nodes[0]
            if old_node.get("id") == new_node.get("id"):
                continue
            if require_same_level and old_node.get("level") != new_node.get("level"):
                continue
            used_old.add(id(old_node))
            used_new.add(id(new_node))
            matches[id(new_node)] = old_node

    pair_unique(_distinctive_title_key)
    pair_unique(_child_subject_key)
    if matches:
        # Once a title/structure anchor confirms a shift, exact body content
        # can safely carry neighboring clauses along with it.
        pair_unique(_substantial_body_key)
    else:
        # Avoid normalizing every multi-megabyte body on ordinary maintenance
        # comparisons. With no shift anchor, only IDs absent from the opposite
        # side can possibly be a body-backed renumbering.
        old_ids = {node.get("id") for node in old_clauses}
        new_ids = {node.get("id") for node in new_clauses}
        old_missing = [node for node in old_clauses if node.get("id") not in new_ids]
        new_missing = [node for node in new_clauses if node.get("id") not in old_ids]
        if old_missing and new_missing:
            pair_unique(_substantial_body_key, old_missing, new_missing)
    _pair_unique_similar_bodies(
        old_clauses,
        new_clauses,
        old_id_counts,
        new_id_counts,
        used_old,
        used_new,
        matches,
    )
    # A confirmed move at this sibling level (or a renumbered parent) gives
    # enough context to safely carry along a unique generic heading such as
    # "General". A lone generic old/new pair remains deliberately ambiguous.
    parent_moved = bool(
        old_parent_id and new_parent_id and old_parent_id != new_parent_id
    )
    if matches or parent_moved:
        pair_unique(_exact_title_key, require_same_level=not parent_moved)
    if parent_moved:
        pair_unique(_relative_id_key, require_same_level=False)
    return matches


def _pair_unique_similar_bodies(
    old_clauses: list,
    new_clauses: list,
    old_id_counts: Counter,
    new_id_counts: Counter,
    used_old: set,
    used_new: set,
    matches: dict,
):
    """Pair duplicate/changed IDs only when substantial bodies disambiguate.

    Some published documents repeat an identifier for two sibling headings and
    fix the second identifier in the next release. Titles alone can be
    misleading in that shape, so require a strong body score and a unique
    candidate in both directions. Large bodies are skipped to keep comparison
    latency bounded.
    """
    max_body_chars = 50_000
    old_candidates = [
        node for node in old_clauses
        if id(node) not in used_old
        and _eligible_renumber_node(node)
        and old_id_counts[node.get("id", "")] > new_id_counts[node.get("id", "")]
    ]
    new_candidates = [
        node for node in new_clauses
        if id(node) not in used_new
        and _eligible_renumber_node(node)
        and new_id_counts[node.get("id", "")] > old_id_counts[node.get("id", "")]
    ]
    if not old_candidates or not new_candidates:
        return

    old_bodies = {
        id(node): _normalize_whitespace(node.get("body", ""))
        for node in old_candidates
    }
    new_bodies = {
        id(node): _normalize_whitespace(node.get("body", ""))
        for node in new_candidates
    }
    by_old = defaultdict(list)
    by_new = defaultdict(list)
    for old_node in old_candidates:
        old_body = old_bodies[id(old_node)]
        if not 120 <= len(old_body) <= max_body_chars:
            continue
        old_title = _title_for_matching(old_node)
        for new_node in new_candidates:
            if old_node.get("level") != new_node.get("level"):
                continue
            new_body = new_bodies[id(new_node)]
            if not 120 <= len(new_body) <= max_body_chars:
                continue
            if min(len(old_body), len(new_body)) / max(len(old_body), len(new_body)) < 0.55:
                continue
            title_score = difflib.SequenceMatcher(
                None, old_title, _title_for_matching(new_node)
            ).ratio()
            if title_score < 0.45:
                continue
            body_score = difflib.SequenceMatcher(None, old_body, new_body).ratio()
            if body_score < 0.82:
                continue
            pair = (old_node, new_node)
            by_old[id(old_node)].append(pair)
            by_new[id(new_node)].append(pair)

    for pairs in by_old.values():
        if len(pairs) != 1:
            continue
        old_node, new_node = pairs[0]
        if len(by_new[id(new_node)]) != 1:
            continue
        if id(old_node) in used_old or id(new_node) in used_new:
            continue
        used_old.add(id(old_node))
        used_new.add(id(new_node))
        matches[id(new_node)] = old_node


def _eligible_renumber_node(node: dict) -> bool:
    return not node.get("id", "").casefold().startswith("annex ")


def _distinctive_title_key(node: dict, _parent_id: str):
    if not _eligible_renumber_node(node):
        return None
    title = _title_for_matching(node)
    if len(title) < 12 or title in _GENERIC_RENUMBER_TITLES:
        return None
    return title


def _exact_title_key(node: dict, _parent_id: str):
    if not _eligible_renumber_node(node):
        return None
    return _title_for_matching(node) or None


def _child_subject_key(node: dict, _parent_id: str):
    """Fingerprint an otherwise empty container by its direct child subjects."""
    if not _eligible_renumber_node(node):
        return None
    subjects = tuple(_title_for_matching(child) for child in node.get("children", []))
    if len(subjects) < 2 or sum(map(len, subjects)) < 30:
        return None
    return subjects


def _substantial_body_key(node: dict, _parent_id: str):
    if not _eligible_renumber_node(node):
        return None
    body = _normalize_whitespace(node.get("body", ""))
    if len(body) < 120:
        return None
    return body


def _relative_id_key(node: dict, parent_id: str):
    clause_id = node.get("id", "")
    folded_parent = parent_id.casefold()
    if folded_parent.startswith("annex "):
        folded_parent = folded_parent[6:].strip()
    prefix = folded_parent + "."
    folded_id = clause_id.casefold()
    if not folded_parent or not folded_id.startswith(prefix):
        return None
    return folded_id[len(prefix):]


def _build_id_map(clauses: list) -> dict:
    """Build a sibling-level id -> nodes mapping, preserving duplicates."""
    mapping = defaultdict(list)
    for node in clauses:
        nid = node["id"]
        if nid:
            mapping[nid].append(node)
    return dict(mapping)


def _select_old_node(
    candidates: list,
    new_node: dict,
    processed_old_ids: set,
    require_title_similarity: bool,
):
    """Select at most one old sibling for a new clause.

    Duplicate or malformed clause IDs occur in some source documents. Title
    similarity prevents one old clause from being paired with several unrelated
    new clauses that happen to share that parsed ID.
    """
    available = [node for node in candidates if id(node) not in processed_old_ids]
    if not available:
        return None
    if len(available) == 1 and not require_title_similarity:
        return available[0]

    new_title = _title_for_matching(new_node)
    scored = [
        (difflib.SequenceMatcher(None, _title_for_matching(node), new_title).ratio(), node)
        for node in available
    ]
    score, best = max(scored, key=lambda item: item[0])
    if require_title_similarity and score < 0.55:
        structural = sorted(
            [(_child_subject_overlap(node, new_node), node) for node in available],
            key=lambda item: item[0],
            reverse=True,
        )
        structural_score, structural_best = structural[0]
        runner_up = structural[1][0] if len(structural) > 1 else 0.0
        if structural_score >= 0.65 and structural_score - runner_up >= 0.15:
            return structural_best
        new_body = _substantial_body_key(new_node, "")
        if new_body is not None:
            body_matches = [
                node for node in available
                if _substantial_body_key(node, "") == new_body
            ]
            if len(body_matches) == 1:
                return body_matches[0]
        return None
    return best


def _select_renamed_annex(old_clauses: list, new_node: dict, processed_old_ids: set):
    """Match a renumbered annex only when its subject remains unmistakable."""
    new_title = _title_for_matching(new_node)
    if len(new_title) < 8:
        return None
    candidates = []
    for old_node in old_clauses:
        if id(old_node) in processed_old_ids:
            continue
        if not old_node.get("id", "").casefold().startswith("annex "):
            continue
        score = difflib.SequenceMatcher(
            None, _title_for_matching(old_node), new_title
        ).ratio()
        candidates.append((score, old_node))
    if not candidates:
        return None
    candidates.sort(key=lambda item: item[0], reverse=True)
    score, best = candidates[0]
    runner_up = candidates[1][0] if len(candidates) > 1 else 0
    return best if score >= 0.82 and score - runner_up >= 0.1 else None


def _title_for_matching(node: dict) -> str:
    """Normalize a heading and remove its repeated clause identifier."""
    title = re.sub(r"\s+", " ", node.get("title", "")).strip().casefold()
    clause_id = re.sub(r"\s+", " ", node.get("id", "")).strip().casefold()
    if clause_id and title.startswith(clause_id):
        remainder = title[len(clause_id):].lstrip(" .:-")
        if remainder:
            # Annex qualifiers such as "(informative)" describe the document
            # role, not the subject, and otherwise inflate unrelated scores.
            return re.sub(r"^\([^)]+\)\s*:?\s*", "", remainder)
    return title


def _is_modified(old_node: dict, new_node: dict) -> bool:
    """Check all user-visible clause content for substantive changes."""
    old_body = old_node.get("body", "").strip()
    new_body = new_node.get("body", "").strip()
    body_changed = not _whitespace_equal(old_body, new_body)
    title_changed = _title_content(old_node) != _title_content(new_node)
    images_changed = _image_signatures(old_node) != _image_signatures(new_node)
    return body_changed or title_changed or images_changed


def _normalize_whitespace(text: str) -> str:
    # str.split() recognizes Unicode whitespace and runs in optimized C code.
    # It is substantially faster than rewriting multi-megabyte clause bodies
    # through the regex engine.
    return " ".join(text.split())


def _whitespace_equal(left: str, right: str) -> bool:
    """Compare text while treating every whitespace run as one separator."""
    if left == right:
        return True
    return left.split() == right.split()


def _title_content(node: dict) -> str:
    """Normalize a title while ignoring its repeated clause identifier."""
    title = _normalize_whitespace(node.get("title", ""))
    clause_id = _normalize_whitespace(node.get("id", ""))
    if clause_id and title.casefold().startswith(clause_id.casefold()):
        remainder = title[len(clause_id):].lstrip(" .:-–—")
        if remainder:
            return remainder
    return title


def _image_signatures(node: dict) -> tuple:
    """Return stable figure identities across independently packaged releases."""
    signatures = []
    for image in node.get("images", []):
        digest = image.get("sha256")
        if digest:
            signatures.append(("sha256", digest))
        else:
            # Legacy parsed structures do not have content hashes. Keep a
            # deterministic fallback so additions/removals are still detected.
            signatures.append(("source", image.get("id") or image.get("src", "")))
    return tuple(signatures)


def compute_line_diff(old_text: str, new_text: str) -> list:
    """Line-by-line diff."""
    old_lines = old_text.split("\n")
    new_lines = new_text.split("\n")
    matcher = difflib.SequenceMatcher(None, old_lines, new_lines)

    result = []
    for op, i1, i2, j1, j2 in matcher.get_opcodes():
        if op == "equal":
            for line in old_lines[i1:i2]:
                result.append({"type": "equal", "text": line})
        elif op == "insert":
            for line in new_lines[j1:j2]:
                result.append({"type": "insert", "text": line})
        elif op == "delete":
            for line in old_lines[i1:i2]:
                result.append({"type": "delete", "text": line})
        elif op == "replace":
            for line in old_lines[i1:i2]:
                result.append({"type": "delete", "text": line})
            for line in new_lines[j1:j2]:
                result.append({"type": "insert", "text": line})
    return result


def compute_diff_stats(diff_tree: list) -> dict:
    """Count added/deleted/modified/unchanged clauses (recursive)."""
    stats = {"added": 0, "deleted": 0, "modified": 0, "unchanged": 0}

    def walk(nodes):
        for node in nodes:
            s = node.get("status", "unchanged")
            if s in stats:
                stats[s] += 1
            walk(node.get("children", []))

    walk(diff_tree)
    return stats


def flatten_diff(diff_tree: list) -> list:
    """Flatten a diff tree into a list (DFS pre-order)."""
    result = []
    for node in diff_tree:
        # Strip internal keys
        clean = {k: v for k, v in node.items() if not k.startswith("_")}
        result.append(clean)
        result.extend(flatten_diff(node.get("children", [])))
    return result
