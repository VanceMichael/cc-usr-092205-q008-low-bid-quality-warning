import unittest
from pathlib import Path

from src.assessment import AssessmentEngine, HYPOTHESES
from src.case import load_case

CASE_PATH = Path("fixtures/case.json")
REVIEWER = "审查人员"
HANDLER = "经办人"


def engine(role: str = REVIEWER) -> AssessmentEngine:
    return AssessmentEngine(load_case(CASE_PATH), viewer_role=role)


def points(hypothesis: dict, bucket: str) -> list[str]:
    return [p["text"] for p in hypothesis[bucket]]


def all_text(snapshot: dict, bucket: str) -> list[str]:
    out = []
    for hyp in snapshot["hypotheses"].values():
        out.extend(points(hyp, bucket))
    return out


class LowBidFactTest(unittest.TestCase):
    def test_lowest_bidder_tracks_bid_versions(self):
        eng = engine()
        # 只有第一轮报价时：B1 800、B2 680
        self.assertEqual(eng.lowest_bidder("2026-02-01"), "B2")
        # B2 第二轮报价 610 后仍为最低
        self.assertEqual(eng.lowest_bidder("2026-02-09"), "B2")

    def test_price_fact_is_measurement_not_verdict(self):
        eng = engine()
        snap = eng.assess("B2", "2026-02-19")
        # 主材下限 = 120kg × 5.2 = 624，报价 610，每件低 14
        self.assertEqual(snap["price_fact"]["material_floor"], 624)
        self.assertEqual(snap["price_fact"]["gap"], -14)
        joined = "\n".join(all_text(snap, "supporting"))
        self.assertIn("低于按同期参考价测算的主材成本下限", joined)

    def test_first_round_price_did_not_trigger_gap(self):
        eng = engine()
        # 2026-02-01 时 B2 报价 680，高于下限 624
        events = eng.log.visible(REVIEWER, "2026-02-01")
        self.assertTrue(any(e["id"] == "e04" for e in events))
        self.assertFalse(any(e["id"] == "e05" for e in events))


class OutOfOrderAndHistoryTest(unittest.TestCase):
    def test_late_enforcement_updates_judgement_but_keeps_prior_basis(self):
        eng = engine()
        first = eng.assess("B2", "2026-02-19")
        # 迟到归集的执法/质量通报 02-22 才接收，首份快照不含
        self.assertNotIn("e14", first["based_on"])
        self.assertNotIn("e15", first["based_on"])
        first_text = "\n".join(all_text(first, "supporting"))
        self.assertNotIn("低于成本报价", first_text)

        second = eng.assess("B2", "2026-02-23")
        second_text = "\n".join(all_text(second, "supporting"))
        self.assertIn("关联企业 B3", second_text)
        self.assertIn("低标号材料替代", second_text)

        # 首份快照依据保持不变
        stored_first = eng.history[0]
        self.assertEqual(stored_first["snapshot_id"], first["snapshot_id"])
        self.assertEqual(stored_first["based_on"], first["based_on"])
        self.assertNotIn("e14", stored_first["based_on"])
        # 新快照指向前序快照，形成可追溯链条
        self.assertEqual(second["prior_snapshot"], first["snapshot_id"])

    def test_returned_snapshot_cannot_mutate_history(self):
        eng = engine()
        snap = eng.assess("B2", "2026-02-19")
        snap["hypotheses"]["unsustainable_undercutting"]["supporting"].clear()
        stored = eng.history[0]
        self.assertTrue(
            stored["hypotheses"]["unsustainable_undercutting"]["supporting"]
        )

    def test_affiliate_record_is_not_attributed_to_subject(self):
        eng = engine()
        snap = eng.assess("B2", "2026-02-23")
        text = "\n".join(all_text(snap, "supporting"))
        self.assertIn("关联记录，不视为本主体行为", text)
        self.assertEqual(snap["affiliates"], ["B3"])


class AuthorizationTest(unittest.TestCase):
    def test_confidential_cost_visible_only_to_authorized_role(self):
        reviewer = engine(REVIEWER).assess("B2", "2026-02-19")
        # 授权角色可见锁价协议：报价 610 可覆盖锁价下限 588
        promo = points(reviewer["hypotheses"]["short_term_promotion"], "supporting")
        self.assertTrue(any("锁价协议单价 4.9" in t for t in promo))
        self.assertFalse(reviewer["redacted_for_role"])

    def test_unauthorized_role_gets_redaction_notice_without_content(self):
        eng = engine(HANDLER)
        snap = eng.assess("B2", "2026-02-19")
        self.assertNotIn("e06", snap["based_on"])
        self.assertIn(
            {"event": "e06", "type": "verifiable_cost"}, snap["redacted_for_role"]
        )
        promo = points(snap["hypotheses"]["short_term_promotion"], "supporting")
        self.assertFalse(any("锁价协议单价" in t for t in promo))

    def test_appeal_supplement_unverified_is_open_question(self):
        eng = engine()
        snap = eng.assess("B2", "2026-02-26")
        questions = "\n".join(p["text"] for p in snap["open_questions"])
        self.assertIn("申诉材料待核验", questions)
        self.assertIn("省材工艺专利", questions)
        counters = "\n".join(all_text(snap, "counter"))
        self.assertNotIn("申诉材料已经核验", counters)


class QueueAndWaiverTest(unittest.TestCase):
    def test_waiver_then_expiry(self):
        eng = engine()
        active = eng.assess("B2", "2026-02-26")
        self.assertIn("人工豁免观察期", active["queue"]["status"])

        expired = eng.assess("B2", "2026-03-06")
        self.assertIn("豁免已到期，自动回到复核队列", expired["queue"]["status"])

    def test_failed_delivery_during_waiver_returns_early(self):
        eng = engine()
        snap = eng.assess("B2", "2026-06-25")
        self.assertIn("观察期内出现不合格交付，提前回到复核队列", snap["queue"]["status"])


class HypothesisCorrectionTest(unittest.TestCase):
    def test_failed_delivery_strengthens_direction_without_determination(self):
        eng = engine()
        snap = eng.assess("B2", "2026-07-01")
        uns = snap["hypotheses"]["unsustainable_undercutting"]
        eff = snap["hypotheses"]["efficiency_advantage"]
        self.assertTrue(any("交付检验不合格" in t for t in points(uns, "supporting")))
        self.assertTrue(any("效率优势解释被削弱" in t for t in points(eff, "counter")))
        self.assertIn("同向不合格结果", uns["standing"])
        # 原因调查仍被列为待补证据，而非直接定性
        questions = "\n".join(p["text"] for p in snap["open_questions"])
        self.assertIn("原因调查", questions)

    def test_remediated_delivery_weakens_signal(self):
        eng = engine()
        snap = eng.assess("B2", "2026-08-20")
        uns = snap["hypotheses"]["unsustainable_undercutting"]
        self.assertIn("整改后交付合格，信号减弱", uns["standing"])
        self.assertTrue(any("整改后第二、三批交付检验合格" in t for t in points(uns, "counter")))

    def test_price_shock_after_bid_updates_pressure(self):
        eng = engine()
        snap = eng.assess("B2", "2026-03-11")
        text = "\n".join(all_text(snap, "supporting"))
        # 主材下限随涨价升至 120 × 5.8 = 696
        self.assertIn("主材成本下限升至 696.0", text)


class PresentationTest(unittest.TestCase):
    def test_output_is_candidate_explanations_not_score(self):
        snap = engine().assess("B2", "2026-03-11")
        self.assertEqual(set(snap["hypotheses"]), set(HYPOTHESES))
        flat = repr(snap)
        self.assertNotIn("score", flat)
        self.assertNotIn("分数", flat)
        self.assertTrue(snap["open_questions"])
        self.assertIn("不对任何主体作违规定性", snap["disclaimer"])

    def test_every_hypothesis_has_standing_and_questions(self):
        snap = engine().assess("B2", "2026-03-11")
        for hyp in snap["hypotheses"].values():
            self.assertIn("standing", hyp)
            self.assertTrue(hyp["label"])


if __name__ == "__main__":
    unittest.main()
