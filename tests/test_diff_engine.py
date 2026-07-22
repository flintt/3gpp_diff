import unittest

from diff_engine import compute_diff_stats, diff_trees


def clause(identifier, title, body="", children=None):
    return {
        "id": identifier,
        "title": title,
        "level": 1,
        "body": body,
        "children": children or [],
    }


class DiffTreeMatchingTests(unittest.TestCase):
    def test_matches_a_normal_clause(self):
        result = diff_trees(
            [clause("1", "1 General", "old wording")],
            [clause("1", "1 General", "new wording")],
        )

        self.assertEqual(result[0]["status"], "modified")
        self.assertEqual(result[0]["old_body"], "old wording")
        self.assertNotIn("_sort_key", result[0])

    def test_duplicate_ids_are_matched_only_once(self):
        result = diff_trees(
            [clause("5.27.1.1", "5.27.1.1 General", "same")],
            [
                clause("5.27.1.1", "5.27.1.1 General", "same"),
                clause(
                    "5.27.1.1",
                    "5.27.1.1 Controlling time synchronization service",
                    "new clause",
                ),
            ],
        )

        self.assertEqual([node["status"] for node in result], ["unchanged", "added"])
        self.assertEqual(
            compute_diff_stats(result),
            {"added": 1, "deleted": 0, "modified": 0, "unchanged": 1},
        )

    def test_does_not_match_a_descendant_at_the_wrong_level(self):
        result = diff_trees(
            [clause("1", "1 Parent", children=[clause("2", "2 Nested", "old")])],
            [clause("2", "2 Top level", "new")],
        )

        self.assertEqual([node["status"] for node in result], ["deleted", "added"])

    def test_preserves_document_order_for_annex_clauses(self):
        old = [
            clause("1", "1 Scope"),
            clause("2", "2 References"),
            clause("Annex D", "Annex D (informative): deployment options"),
            clause("D.5", "D.5 Overlay network support"),
        ]
        new = [dict(node) for node in old]

        result = diff_trees(old, new)

        self.assertEqual(
            [node["id"] for node in result],
            ["1", "2", "Annex D", "D.5"],
        )

    def test_keeps_deleted_clause_near_its_original_position(self):
        result = diff_trees(
            [
                clause("1", "1 Scope"),
                clause("2", "2 Removed section"),
                clause("3", "3 Definitions"),
                clause("Annex A", "Annex A (informative): notes"),
            ],
            [
                clause("1", "1 Scope"),
                clause("3", "3 Definitions"),
                clause("Annex A", "Annex A (informative): notes"),
            ],
        )

        self.assertEqual(
            [(node["id"], node["status"]) for node in result],
            [
                ("1", "unchanged"),
                ("2", "deleted"),
                ("3", "unchanged"),
                ("Annex A", "unchanged"),
            ],
        )


if __name__ == "__main__":
    unittest.main()
