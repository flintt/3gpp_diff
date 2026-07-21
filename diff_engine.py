"""
Clause-by-clause diff engine for 3GPP specifications.
Compares two structured clause trees and produces a diff tree.
"""
import difflib
import re


def diff_trees(old_clauses: list, new_clauses: list) -> list:
    """
    Compare two clause trees and produce a diff tree.
    Each node gets a status: 'unchanged' | 'modified' | 'added' | 'deleted'.

    Matching:
      1. By clause ID (e.g. "4.2.3")
      2. Unmatched old  -> deleted
      3. Unmatched new  -> added

    This function only processes top-level nodes; recursion handles children.
    """
    old_by_id = _build_id_map(old_clauses)
    new_by_id = _build_id_map(new_clauses)

    result = []
    processed_old_ids = set()

    # Process new clauses in document order, matching against old
    for new_node in new_clauses:
        nid = new_node["id"]
        key = _clause_sort_key(nid)

        old_node = old_by_id.get(nid)

        if old_node is not None:
            processed_old_ids.add(id(old_node))
            children = diff_trees(
                old_node.get("children", []),
                new_node.get("children", []),
            )
            if _is_modified(old_node, new_node):
                result.append({
                    "id": nid,
                    "title": new_node["title"],
                    "level": new_node["level"],
                    "status": "modified",
                    "old_body": old_node.get("body", ""),
                    "new_body": new_node.get("body", ""),
                    "old_images": old_node.get("images", []),
                    "new_images": new_node.get("images", []),
                    "_sort_key": key,
                    "children": children,
                })
            else:
                result.append({
                    "id": nid,
                    "title": new_node["title"],
                    "level": new_node["level"],
                    "status": "unchanged",
                    "body": new_node.get("body", ""),
                    "images": new_node.get("images", []),
                    "_sort_key": key,
                    "children": children,
                })
        else:
            # New clause not in old -> added
            result.append({
                "id": nid,
                "title": new_node["title"],
                "level": new_node["level"],
                "status": "added",
                "body": new_node.get("body", ""),
                "images": new_node.get("images", []),
                "_sort_key": key,
                "children": _mark_all_added(new_node.get("children", [])),
            })

    # Old clauses that were never matched -> deleted
    for old_node in old_clauses:
        if id(old_node) not in processed_old_ids:
            result.append({
                "id": old_node["id"],
                "title": old_node["title"],
                "level": old_node["level"],
                "status": "deleted",
                "body": old_node.get("body", ""),
                "images": old_node.get("images", []),
                "_sort_key": _clause_sort_key(old_node["id"]),
                "children": _mark_all_deleted(old_node.get("children", [])),
            })

    # Sort by document order
    result.sort(key=lambda x: x.get("_sort_key", (9999,)))
    for node in result:
        node.pop("_sort_key", None)
    return result


def _clause_sort_key(clause_id: str):
    """Generate sort key from '4.2.3' -> (4, 2, 3)."""
    try:
        parts = clause_id.split(".")
        # Handle cases like '5. 35A.1' -> filter non-numeric
        nums = []
        for p in parts:
            m = re.match(r"(\d+)", p)
            if m:
                nums.append(int(m.group(1)))
            else:
                nums.append(0)
        return tuple(nums)
    except (ValueError, AttributeError):
        return (9999,)


def _build_id_map(clauses: list) -> dict:
    """Build flat id -> node mapping."""
    mapping = {}
    for node in clauses:
        nid = node["id"]
        if nid:
            mapping[nid] = node
        mapping.update(_build_id_map(node.get("children", [])))
    return mapping


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
