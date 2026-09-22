import unittest
from pathlib import Path

from src.assessment import (
    AssessmentEngine,
    Clearance,
    Fact,
    load_case,
)

FIXTURE = Path("fixtures/assessment.json")


def by_key(snapshot, key):
    for candidate in snapshot.candidates:
        if candidate.key == key:
            return candidate
    raise KeyError(key)


class AssessmentTest(unittest.TestCase):
    def setUp(self):
        self.engine = load_case(FIXTURE)

    def test_candidate_explanations_are_issued_without_verdict(self):
        snap = self.engine.assess("2026-09-15T09:00:00", Clearance.INTERNAL)
        keys = {c.key for c in snap.candidates}
        # 输出的是候选解释,不是单一分数或定性结论。
        self.assertIn("甲公司:efficiency", keys)
        self.assertIn("甲公司:promotion", keys)
        self.assertIn("甲公司:unsustainable", keys)
        self.assertIn("甲公司:substitution", keys)
        allowed = {"效率优势(工艺/管理带来的真实成本下降)", "短期促销(阶段性让利换取市场)",
                   "不可持续压价(低于可验证成本且无合理来源)", "交付偷换材料风险", "关联企业协同投标风险"}
        self.assertTrue(all(c.label in allowed for c in snap.candidates))

    def test_below_cost_creates_suspicion_and_missing_evidence(self):
        snap = self.engine.assess("2026-09-15T09:00:00", Clearance.INTERNAL)
        unsustainable = by_key(snap, "甲公司:unsustainable")
        self.assertTrue(unsustainable.supports)
        self.assertTrue(any("可验证主材成本下限" in e.reason for e in unsustainable.supports))
        self.assertTrue(unsustainable.missing)

    def test_counterevidence_is_retained_not_overwritten(self):
        snap = self.engine.assess("2026-09-15T09:00:00", Clearance.INTERNAL)
        substitution = by_key(snap, "甲公司:substitution")
        # 历史投诉查实是疑点,既往检验合格作为反证同时保留。
        self.assertTrue(any("投诉经查实" in e.reason for e in substitution.supports))
        self.assertTrue(any("进场材料检验与承诺规格一致" in e.reason for e in substitution.contradicts))

    def test_superseded_bid_version_is_excluded_but_kept_in_history(self):
        snap = self.engine.assess("2026-09-15T09:00:00", Clearance.INTERNAL)
        self.assertNotIn("bid-a1", snap.facts_considered)
        self.assertIn("bid-a1", snap.superseded_facts)

    def test_trade_secret_redacted_without_authorization(self):
        internal = self.engine.assess("2026-09-15T09:00:00", Clearance.INTERNAL)
        secret = self.engine.assess("2026-09-15T09:00:00", Clearance.TRADE_SECRET)
        self.assertGreaterEqual(internal.excluded_secret_count, 1)
        self.assertEqual(secret.excluded_secret_count, 0)
        promotion = by_key(secret, "甲公司:promotion")
        self.assertTrue(any("阶段性让价" in e.reason for e in promotion.supports))

    def test_out_of_order_events_update_but_prior_revision_kept(self):
        before = self.engine.assess("2026-09-15T09:00:00", Clearance.INTERNAL)
        # 价格事件发生于 09-10,但 09-18 才到达:09-15 的研判不能提前使用它。
        self.assertFalse(
            any("主材价格上涨" in e.reason for c in before.candidates for e in c.supports)
        )
        after = self.engine.assess("2026-09-19T12:00:00", Clearance.INTERNAL)
        self.assertEqual(after.revision, 2)
        self.assertTrue(
            any("主材价格上涨" in e.reason
                for c in after.candidates for e in c.supports)
        )
        # 旧版本及其依据完整保留。
        self.assertEqual(self.engine.revisions[0].revision, 1)
        self.assertEqual(self.engine.revisions[0].generated_at.isoformat(),
                         "2026-09-15T09:00:00")

    def test_exemption_expiry_returns_bidder_to_queue(self):
        exempt = self.engine.assess("2026-09-15T09:00:00", Clearance.INTERNAL)
        self.assertIn("甲公司", {q.bidder for q in exempt.exempted})
        self.assertNotIn("甲公司", {q.bidder for q in exempt.queue})

        expired = self.engine.assess("2026-09-21T09:00:00", Clearance.INTERNAL)
        self.assertIn("甲公司", {q.bidder for q in expired.queue})
        self.assertTrue(
            any("豁免" in r and "到期" in r
                for q in expired.queue if q.bidder == "甲公司" for r in q.reasons)
        )
        self.assertNotIn("甲公司", {q.bidder for q in expired.exempted})

    def test_affiliated_bidders_flag_collusion_with_inconclusive_gap(self):
        snap = self.engine.assess("2026-09-15T09:00:00", Clearance.INTERNAL)
        collusion = by_key(snap, "甲公司:collusion")
        self.assertTrue(any("关联方" in e.reason and "丙公司" in e.reason
                            for e in collusion.supports))
        self.assertTrue(collusion.missing)
        self.assertNotEqual(collusion.standing, "存在反证,暂不成立")

    def test_joint_bid_is_visible_as_fact_to_verify(self):
        snap = self.engine.assess("2026-09-15T09:00:00", Clearance.INTERNAL)
        collusion = by_key(snap, "乙公司:collusion")
        self.assertTrue(any("联合投标" in e.reason for e in collusion.supports))

    def test_delivery_quality_calibrates_after_award(self):
        self.engine.assess("2026-09-21T09:00:00", Clearance.INTERNAL)
        self.engine.add_fact(Fact.from_dict({
            "fact_id": "fb-1",
            "category": "delivery_feedback",
            "occurred_at": "2026-10-10T09:00:00",
            "payload": {"bidder": "甲公司", "result": "fail",
                        "material_mismatch": True, "note": "首批进场材料与封样不符"},
            "source": "交付检验",
        }))
        snap = self.engine.assess("2026-10-11T09:00:00", Clearance.INTERNAL)
        self.assertIn("甲公司:substitution", snap.calibration.strengthened)
        self.assertTrue(any("封样不符" in n for n in snap.calibration.new_findings))

    def test_duplicate_fact_rejected(self):
        with self.assertRaises(ValueError):
            self.engine.add_fact({
                "fact_id": "bid-a1",
                "category": "bid_version",
                "occurred_at": "2026-09-01T10:00:00",
                "payload": {"bidder": "甲公司", "version": 3, "unit_price": 90},
            })

    def test_snapshot_serializable(self):
        snap = self.engine.assess("2026-09-15T09:00:00", Clearance.PUBLIC)
        import json
        json.dumps(snap.to_dict(), ensure_ascii=False)


if __name__ == "__main__":
    unittest.main()
