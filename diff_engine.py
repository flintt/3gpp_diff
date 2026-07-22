"""
Clause-by-clause diff engine for 3GPP specifications.
Compares two structured clause trees and produces a diff tree.
"""
import difflib
import re
from collections import Counter, defaultdict


def diff_trees(old_clauses: list, new_clauses: list) -> list:
    """
    Compare two clause trees and produce a diff tree.
    Each node gets a status: 'unchanged' | 'modified' | 'added' | 'deleted'.

    Matching:
      1. By clause ID (e.g. "4.2.3")
      2. Unmatched old  -> deleted
      3. Unmatched new  -> added

    Ordering follows the new document. Deleted clauses are inserted before
    their next surviving old-document sibling so they remain near their
    original position.

    This function only processes top-level nodes; recursion handles children.
    """
    old_by_id = _build_id_map(old_clauses)
    new_id_counts = Counter(node.get("id", "") for node in new_clauses)

    result = []
    processed_old_ids = set()
    matched_results = {}

    # Process new clauses in document order, matching against old
    for new_node in new_clauses:
        nid = new_node["id"]

        candidates = old_by_id.get(nid, [])
        old_node = _select_old_node(
            candidates,
            new_node,
            processed_old_ids,
            require_title_similarity=(len(candidates) > 1 or new_id_counts[nid] > 1),
        )

        if old_node is not None:
            processed_old_ids.add(id(old_node))
            children = diff_trees(
                old_node.get("children", []),
                new_node.get("children", []),
            )
            if _is_modified(old_node, new_node):
                result_node = {
                    "id": nid,
                    "title": new_node["title"],
                    "level": new_node["level"],
                    "status": "modified",
                    "old_body": old_node.get("body", ""),
                    "new_body": new_node.get("body", ""),
                    "old_images": old_node.get("images", []),
                    "new_images": new_node.get("images", []),
                    "children": children,
                }
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
                "children": _mark_all_added(new_node.get("children", [])),
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
                "children": _mark_all_deleted(old_node.get("children", [])),
            })
        elif pending_deleted:
            anchor = matched_results[id(old_node)]
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
        return None
    return best


def _title_for_matching(node: dict) -> str:
    """Normalize a heading and remove its repeated clause identifier."""
    title = re.sub(r"\s+", " ", node.get("title", "")).strip().casefold()
    clause_id = re.sub(r"\s+", " ", node.get("id", "")).strip().casefold()
    if clause_id and title.startswith(clause_id):
        remainder = title[len(clause_id):].lstrip(" .:-")
        if remainder:
            return remainder
    return title


def _is_modified(old_node: dict, new_node: dict) -> bool:
    """Check if clause body content has changed."""
    old_body = old_node.get("body", "").strip()
    new_body = new_node.get("body", "").strip()
    if old_body == new_body:
        return False
    # Check whitespace-only differences
    if re.sub(r'\s+', ' ', old_body) == re.sub(r'\s+', ' ', new_body):
        return False
    return True


def _mark_all_added(clauses: list) -> list:
    """Recursively mark all nodes as added."""
    return [
        {
            "id": n["id"],
            "title": n["title"],
            "level": n["level"],
            "status": "added",
            "body": n.get("body", ""),
            "images": n.get("images", []),
            "children": _mark_all_added(n.get("children", [])),
        }
        for n in clauses
    ]


def _mark_all_deleted(clauses: list) -> list:
    """Recursively mark all nodes as deleted."""
    return [
        {
            "id": n["id"],
            "title": n["title"],
            "level": n["level"],
            "status": "deleted",
            "body": n.get("body", ""),
            "images": n.get("images", []),
            "children": _mark_all_deleted(n.get("children", [])),
        }
        for n in clauses
    ]


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
