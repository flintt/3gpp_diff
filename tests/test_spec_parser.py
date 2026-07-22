import unittest

from diff_engine import diff_trees
from spec_parser import _build_tree, _extract_clause_id


class ClauseIdParsingTests(unittest.TestCase):
    def test_extracts_annex_clause_ids(self):
        self.assertEqual(
            _extract_clause_id(
                "D.5 Support for keeping UE in CM-CONNECTED state "
                "in overlay network when accessing services via NWu"
            ),
            "D.5",
        )
        self.assertEqual(_extract_clause_id("D.7.1 Network initiated QoS"), "D.7.1")
        self.assertEqual(
            _extract_clause_id("Annex D (informative): deployment options"),
            "Annex D",
        )

    def test_normalizes_spaces_inside_numeric_clause_ids(self):
        self.assertEqual(_extract_clause_id("5.2. 1 General"), "5.2.1")
        self.assertEqual(_extract_clause_id("4 . 3 Security aspects"), "4.3")
        self.assertEqual(_extract_clause_id("5.35A.1 General"), "5.35A.1")

    def test_title_spacing_change_is_a_modification_not_add_delete(self):
        old_tree = _build_tree([
            (
                1,
                "D.5 Support for keeping UE in CM-CONNECTED state "
                "in overlay network when accessing services via NWu",
                ["old wording"],
            )
        ])
        new_tree = _build_tree([
            (
                1,
                "D.5 Support for keeping UE in CM-CONNECTED state "
                "in overlay network when accessing services via  NWu",
                ["new wording"],
            )
        ])

        result = diff_trees(old_tree, new_tree)

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["id"], "D.5")
        self.assertEqual(result[0]["status"], "modified")
        self.assertEqual(result[0]["old_body"], "old wording")
        self.assertEqual(result[0]["new_body"], "new wording")


if __name__ == "__main__":
    unittest.main()
