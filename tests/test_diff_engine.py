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


if __name__ == "__main__":
    unittest.main()
