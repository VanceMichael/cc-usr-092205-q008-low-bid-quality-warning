import copy
import json
import unittest
from pathlib import Path

from src.case import load_case

CASE_PATH = Path("fixtures/case.json")


class CaseLoadingTest(unittest.TestCase):
    def test_fixture_loads(self):
        case = load_case(CASE_PATH)
        self.assertEqual(case["domain"], "low-bid-quality-warning")
        self.assertGreaterEqual(len(case["events"]), 10)

    def test_duplicate_event_id_rejected(self):
        case = load_case(CASE_PATH)
        case["events"][1]["id"] = case["events"][0]["id"]
        self._assert_raises(case, "事件编号缺失或重复")

    def test_unknown_bidder_reference_rejected(self):
        case = load_case(CASE_PATH)
        for event in case["events"]:
            if event["type"] == "bid_version":
                event["payload"]["bidder"] = "B999"
                break
        self._assert_raises(case, "不存在的主体")

    def test_confidential_requires_allowed_roles(self):
        case = load_case(CASE_PATH)
        for event in case["events"]:
            if event.get("confidential"):
                del event["allowed_roles"]
                break
        self._assert_raises(case, "allowed_roles")

    def _assert_raises(self, case: dict, fragment: str):
        import tempfile

        with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".json", delete=False) as f:
            json.dump(case, f)
            path = Path(f.name)
        with self.assertRaises(ValueError) as ctx:
            load_case(path)
        self.assertIn(fragment, str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
