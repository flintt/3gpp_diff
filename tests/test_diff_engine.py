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

    def test_repaired_duplicate_id_uses_unique_similar_body(self):
        proactive = "The RAN proactively reports the configured arrival time and periodicity. " * 8
        reactive = "The RAN reactively reports the observed burst arrival time to the core. " * 8
        result = diff_trees(
            [
                clause(
                    "5.27.2.5.2",
                    "5.27.2.5.2 Proactive RAN feedback for Burst Arrival Time",
                    proactive,
                ),
                clause(
                    "5.27.2.5.2",
                    "5.27.2.5.2 Reactive RAN feedback",
                    reactive,
                ),
            ],
            [
                clause(
                    "5.27.2.5.2",
                    "5.27.2.5.2 Proactive RAN feedback for Burst Arrival Time",
                    proactive,
                ),
                clause(
                    "5.27.2.5.3",
                    "5.27.2.5.3 Reactive RAN feedback for Burst Arrival Time adaptation",
                    reactive + "The report may include an updated timing value.",
                ),
            ],
        )

        self.assertEqual(
            [(node["id"], node["status"], node.get("old_id")) for node in result],
            [
                ("5.27.2.5.2", "unchanged", None),
                ("5.27.2.5.3", "modified", "5.27.2.5.2"),
            ],
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

    def test_detects_a_title_only_change(self):
        result = diff_trees(
            [clause("4.2", "4.2 Old architecture title", "same body")],
            [clause("4.2", "4.2 New architecture title", "same body")],
        )

        self.assertEqual(result[0]["status"], "modified")
        self.assertEqual(result[0]["old_title"], "4.2 Old architecture title")
        self.assertEqual(result[0]["title"], "4.2 New architecture title")

    def test_ignores_heading_and_body_whitespace_only_changes(self):
        result = diff_trees(
            [clause("4.2", "4.2  Architecture title", "same\nbody")],
            [clause("4.2", "4.2 Architecture  title", "same body")],
        )

        self.assertEqual(result[0]["status"], "unchanged")

    def test_ignores_unicode_whitespace_only_changes(self):
        old = [clause("1", "1 Scope", "alpha\u00a0beta\n gamma")]
        new = [clause("1", "1   Scope", "alpha beta\tgamma")]

        result = diff_trees(old, new)

        self.assertEqual(result[0]["status"], "unchanged")

    def test_detects_changed_figure_content_but_not_a_renamed_figure(self):
        old = clause("6", "6 Figures", "same")
        new = clause("6", "6 Figures", "same")
        old["images"] = [{"id": "image1.emf", "sha256": "aaa"}]
        new["images"] = [{"id": "image99.emf", "sha256": "aaa"}]
        self.assertEqual(diff_trees([old], [new])[0]["status"], "unchanged")

        new["images"][0]["sha256"] = "bbb"
        changed = diff_trees([old], [new])[0]
        self.assertEqual(changed["status"], "modified")
        self.assertEqual(changed["old_images"], old["images"])
        self.assertEqual(changed["new_images"], new["images"])

    def test_does_not_pair_reused_annex_letters_with_unrelated_content(self):
        result = diff_trees(
            [clause("Annex S", "Annex S (informative): Change history", "old")],
            [clause("Annex S", "Annex S (informative): Architecture examples", "new")],
        )

        self.assertEqual(
            [(node["status"], node["title"]) for node in result],
            [
                ("deleted", "Annex S (informative): Change history"),
                ("added", "Annex S (informative): Architecture examples"),
            ],
        )

    def test_same_annex_id_uses_child_structure_when_title_is_shortened(self):
        old = [clause(
            "Annex C",
            "Annex C (normative): Access token profile for Method 3 with OAuth token",
            children=[
                clause("C.1", "C.1 General", "old general"),
                clause("C.2", "C.2 Access token profile", "old profile"),
                clause("C.3", "C.3 Obtaining tokens", "old tokens"),
            ],
        )]
        new = [clause(
            "Annex C",
            "Annex C (normative): Access token profile",
            children=[
                clause("C.1", "C.1 General", "new general"),
                clause("C.2", "C.2 Access token profile", "new profile"),
                clause("C.3", "C.3 Obtaining tokens", "new tokens"),
            ],
        )]

        result = diff_trees(old, new)

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["status"], "modified")
        self.assertEqual(
            compute_diff_stats(result),
            {"added": 0, "deleted": 0, "modified": 4, "unchanged": 0},
        )

    def test_tracks_an_annex_renumbered_with_the_same_subject(self):
        result = diff_trees(
            [clause("Annex S", "Annex S (informative): Change history", "old row")],
            [clause("Annex X", "Annex X (informative): Change history", "new row")],
        )

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["status"], "modified")
        self.assertEqual(result[0]["old_id"], "Annex S")
        self.assertEqual(result[0]["id"], "Annex X")

    def test_tracks_sections_shifted_by_an_inserted_number(self):
        old = [
            clause("5.1", "5.1 Existing alpha procedure", "alpha wording"),
            clause("5.2", "5.2 Existing beta procedure", "beta wording"),
        ]
        new = [
            clause("5.1", "5.1 Newly inserted procedure", "new wording"),
            clause("5.2", "5.2 Existing alpha procedure", "alpha wording"),
            clause("5.3", "5.3 Existing beta procedure", "beta wording"),
        ]

        result = diff_trees(old, new)

        self.assertEqual(
            [(node["id"], node["status"], node.get("old_id")) for node in result],
            [
                ("5.1", "added", None),
                ("5.2", "modified", "5.1"),
                ("5.3", "modified", "5.2"),
            ],
        )

    def test_carries_a_generic_heading_with_a_confirmed_number_shift(self):
        old = [
            clause("5.1", "5.1 Existing alpha procedure", "alpha"),
            clause("5.2", "5.2 General", "short old details"),
        ]
        new = [
            clause("5.1", "5.1 Newly inserted procedure", "new"),
            clause("5.2", "5.2 Existing alpha procedure", "alpha"),
            clause("5.3", "5.3 General", "short new details"),
        ]

        result = diff_trees(old, new)

        self.assertEqual(
            [(node["id"], node["status"], node.get("old_id")) for node in result],
            [
                ("5.1", "added", None),
                ("5.2", "modified", "5.1"),
                ("5.3", "modified", "5.2"),
            ],
        )

    def test_uses_relative_child_numbers_after_a_parent_renumber(self):
        old = [clause(
            "5.1",
            "5.1 Existing service resources",
            children=[
                clause("5.1.1", "5.1.1 General", "old details"),
                clause("5.1.2", "5.1.2 Overview", "same details"),
            ],
        )]
        new = [clause(
            "5.2",
            "5.2 Existing service resources",
            children=[
                clause("5.2.1", "5.2.1 General", "new details"),
                clause("5.2.2", "5.2.2 Overview", "same details"),
            ],
        )]
        result = diff_trees(old, new)

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["old_id"], "5.1")
        self.assertEqual(
            [(node["id"], node["status"], node.get("old_id"))
             for node in result[0]["children"]],
            [
                ("5.2.1", "modified", "5.1.1"),
                ("5.2.2", "modified", "5.1.2"),
            ],
        )

    def test_tracks_a_subtree_moved_to_a_different_parent(self):
        old = [clause(
            "5.8",
            "5.8 User Plane Management",
            children=[clause(
                "5.8.2",
                "5.8.2 Functional Description",
                children=[clause(
                    "5.8.2.11",
                    "5.8.2.11 Parameters for N4 session management",
                    children=[
                        clause("5.8.2.11.1", "5.8.2.11.1 General", "old general"),
                        clause("5.8.2.11.2", "5.8.2.11.2 N4 Session Context", "same context"),
                    ],
                )],
            )],
        )]
        new = [clause(
            "5.8",
            "5.8 User Plane Management",
            children=[
                clause(
                    "5.8.2",
                    "5.8.2 Functional Description",
                    children=[clause(
                        "5.8.2.11",
                        "5.8.2.11 Parameters for N4 session management (moved)",
                        "The parameters are described in clause 5.8.5.",
                    )],
                ),
                clause(
                    "5.8.5",
                    "5.8.5 Parameters for N4 session management",
                    children=[
                        clause("5.8.5.1", "5.8.5.1 General", "new general"),
                        clause("5.8.5.2", "5.8.5.2 N4 Session Context", "same context"),
                    ],
                ),
            ],
        )]
        old_moved = old[0]["children"][0]["children"][0]
        new_stub = new[0]["children"][0]["children"][0]
        new_moved = new[0]["children"][1]
        old_moved["level"] = new_stub["level"] = 4
        new_moved["level"] = 3
        for child in old_moved["children"]:
            child["level"] = 5
        for child in new_moved["children"]:
            child["level"] = 4

        result = diff_trees(old, new)
        user_plane_children = result[0]["children"]
        stub = user_plane_children[0]["children"][0]
        moved = user_plane_children[1]

        self.assertEqual(stub["status"], "added")
        self.assertEqual(moved["status"], "modified")
        self.assertEqual(moved["old_id"], "5.8.2.11")
        self.assertEqual(
            [(node["id"], node["status"], node.get("old_id"))
             for node in moved["children"]],
            [
                ("5.8.5.1", "modified", "5.8.2.11.1"),
                ("5.8.5.2", "modified", "5.8.2.11.2"),
            ],
        )
        self.assertEqual(compute_diff_stats(result)["deleted"], 0)

    def test_tracks_a_renamed_subtree_when_old_location_becomes_void(self):
        old = [clause(
            "4",
            "4 System procedures",
            children=[clause(
                "4.15.11",
                "4.15.11 Exposure of Events from UPF",
                children=[
                    clause("4.15.11.1", "4.15.11.1 General", "old general"),
                    clause(
                        "4.15.11.2",
                        "4.15.11.2 Information flow for certain UEs",
                        "old certain flow",
                    ),
                    clause(
                        "4.15.11.3",
                        "4.15.11.3 Information flow for any UE",
                        "old any flow",
                    ),
                ],
            )],
        )]
        new = [clause(
            "4",
            "4 System procedures",
            children=[
                clause(
                    "4.15.4",
                    "4.15.4 Core Network Internal Event Exposure",
                    children=[clause(
                        "4.15.4.5",
                        "4.15.4.5 Exposure of Events from UPF for UPF Data Collection",
                        children=[
                            clause("4.15.4.5.1", "4.15.4.5.1 General", "new general"),
                            clause(
                                "4.15.4.5.2",
                                "4.15.4.5.2 Information flow for subscription for certain UEs",
                                "new certain flow",
                            ),
                            clause(
                                "4.15.4.5.3",
                                "4.15.4.5.3 Information flow for any UE through SMF",
                                "new any flow",
                            ),
                            clause("4.15.4.5.4", "4.15.4.5.4 Information flow for AOI", "new"),
                        ],
                    )],
                ),
                clause("4.15.11", "4.15.11 Void"),
            ],
        )]

        result = diff_trees(old, new)
        moved = result[0]["children"][0]["children"][0]

        self.assertEqual(moved["status"], "modified")
        self.assertEqual(moved["old_id"], "4.15.11")
        self.assertEqual(
            [(node["id"], node["status"], node.get("old_id"))
             for node in moved["children"]],
            [
                ("4.15.4.5.1", "modified", "4.15.11.1"),
                ("4.15.4.5.2", "modified", "4.15.11.2"),
                ("4.15.4.5.3", "modified", "4.15.11.3"),
                ("4.15.4.5.4", "added", None),
            ],
        )
        self.assertEqual(compute_diff_stats(result)["deleted"], 0)

    def test_does_not_cross_parent_match_without_structural_evidence(self):
        old = [clause(
            "1",
            "1 Old container",
            children=[clause("1.1", "1.1 Distinctive service procedure", "short old")],
        )]
        new = [clause("2", "2 Distinctive service procedure", "short new")]

        result = diff_trees(old, new)

        self.assertEqual(
            [(node["id"], node["status"]) for node in result],
            [("1", "deleted"), ("2", "added")],
        )

    def test_does_not_guess_between_ambiguous_generic_renumberings(self):
        result = diff_trees(
            [clause("5.1", "5.1 General", "old short text")],
            [clause("5.2", "5.2 General", "new short text")],
        )

        self.assertEqual(
            [(node["status"], node["id"]) for node in result],
            [("deleted", "5.1"), ("added", "5.2")],
        )

    def test_exact_substantial_body_can_anchor_a_renamed_heading(self):
        body = "The UE shall preserve this procedure state across registration. " * 4
        result = diff_trees(
            [clause("5.1", "5.1 Previous heading", body)],
            [clause("5.2", "5.2 Revised heading", body)],
        )

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["status"], "modified")
        self.assertEqual(result[0]["old_id"], "5.1")
        self.assertEqual(result[0]["id"], "5.2")

    def test_tracks_parent_body_moved_into_a_new_child(self):
        old_body = (
            "The functional security model defines security domains and the "
            "reference points between those domains. " * 8
        )
        new_body = old_body + "The model also identifies the trust boundaries."
        result = diff_trees(
            [clause("5", "5 Functional security model", old_body)],
            [clause(
                "5",
                "5 Functional security model",
                children=[
                    clause(
                        "5.1",
                        "5.1 General functional security model",
                        new_body,
                    ),
                    clause("5.2", "5.2 Supporting RNAA", "New supporting content"),
                ],
            )],
        )

        parent = result[0]
        moved, added = parent["children"]
        self.assertEqual(parent["status"], "unchanged")
        self.assertEqual(parent["body"], "")
        self.assertEqual(moved["status"], "modified")
        self.assertEqual(moved["change_type"], "moved")
        self.assertEqual(moved["old_id"], "5")
        self.assertEqual(moved["id"], "5.1")
        self.assertEqual(moved["old_body"], old_body)
        self.assertEqual(moved["new_body"], new_body)
        self.assertEqual(added["status"], "added")
        self.assertEqual(
            compute_diff_stats(result),
            {"added": 1, "deleted": 0, "modified": 1, "unchanged": 1},
        )

    def test_does_not_guess_when_parent_body_matches_multiple_new_children(self):
        body = (
            "The functional security model defines security domains and the "
            "reference points between those domains. " * 8
        )
        result = diff_trees(
            [clause("5", "5 Functional security model", body)],
            [clause(
                "5",
                "5 Functional security model",
                children=[
                    clause("5.1", "5.1 General functional security model", body),
                    clause("5.2", "5.2 Functional security model details", body),
                ],
            )],
        )

        self.assertEqual(result[0]["status"], "modified")
        self.assertEqual(
            [node["status"] for node in result[0]["children"]],
            ["added", "added"],
        )
        self.assertTrue(all(
            "change_type" not in node for node in result[0]["children"]
        ))


if __name__ == "__main__":
    unittest.main()
