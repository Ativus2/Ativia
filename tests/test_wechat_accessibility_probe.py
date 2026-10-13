"""Offline contracts for chrome false positives and diagnostic uncertainty."""
import copy
import unittest
from tools.wechat_accessibility_probe import classify_target, msaa_semantic_nodes


def chrome_tree():
    # Actual Win32 default: CLIENT -> WINDOW -> seven nonclient/client children.
    nodes = [{"depth": 0, "role": 10}, {"depth": 1, "role": 9}]
    for role in (2, 1, 2, 10, 3, 3, 4):
        nodes.append({"depth": 2, "role": role})
        if role == 1:
            nodes.extend({"depth": 3, "role": 43} for _ in range(5))
        if role == 3:
            nodes.extend({"depth": 3, "role": r} for r in (43, 43, 39, 43, 43))
    return {"status": "OK", "nodes": nodes, "errors": [], "truncated": False}


def report():
    return {
        "uia": {"node_count": 2, "max_depth": 1, "errors": [], "truncated": False},
        "msaa_client": chrome_tree(), "child_clients": [],
        "hit_test": [{"status": "OK", "control_type": 50032}],
        "native_children_truncated": False,
    }


class AccessibilityClassificationTests(unittest.TestCase):
    def test_more_nodes_from_window_chrome_do_not_pass_semantics(self):
        item = report()
        self.assertEqual(len(item["msaa_client"]["nodes"]), 24)
        self.assertEqual(msaa_semantic_nodes(item["msaa_client"]), [])
        self.assertEqual(classify_target(item), "NO_SEMANTIC_STRUCTURE")

    def test_real_client_button_is_not_globally_filtered(self):
        item = report()
        # Client content button: outside title-bar and scrollbar branches.
        nodes = item["msaa_client"]["nodes"]
        client = next(i for i, n in enumerate(nodes) if n == {"depth": 2, "role": 10})
        nodes.insert(client + 1, {"depth": 3, "role": 43})
        self.assertEqual(classify_target(item), "SEMANTIC_CANDIDATE")
        self.assertEqual(item["msaa_client_semantic_node_count"], 1)

    def test_errors_and_truncation_never_become_negative_capability_evidence(self):
        for key in ("errors", "truncated"):
            with self.subTest(key=key):
                item = report()
                item["msaa_client"][key] = [{"type": "COMError"}] if key == "errors" else True
                self.assertEqual(classify_target(item), "TOOL_ERROR")
        item = report()
        item["hit_test"] = [{"status": "TOOL_ERROR"}]
        self.assertEqual(classify_target(item), "TOOL_ERROR")

    def test_unknown_client_role_remains_a_candidate(self):
        item = report()
        item["msaa_client"]["nodes"] = [{"depth": 0, "role": 10}, {"depth": 1, "role": "custom"}]
        self.assertEqual(classify_target(item), "SEMANTIC_CANDIDATE")

    def test_unknown_content_inside_decoration_branch_is_not_silently_discarded(self):
        item = report()
        nodes = item["msaa_client"]["nodes"]
        title = next(i for i, n in enumerate(nodes) if n == {"depth": 2, "role": 1})
        nodes.insert(title + 1, {"depth": 3, "role": "custom"})
        self.assertEqual(classify_target(item), "SEMANTIC_CANDIDATE")
    def test_missing_msaa_interface_is_not_a_successful_negative_probe(self):
        item = report()
        item["msaa_client"] = {"status": "UNAVAILABLE", "nodes": []}
        self.assertEqual(classify_target(item), "TOOL_ERROR")


if __name__ == "__main__":
    unittest.main()


