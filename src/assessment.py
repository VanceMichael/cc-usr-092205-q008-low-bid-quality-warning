"""低价投标风险研判:多源事件归并、候选解释与复核队列。

研判原则:
- 输出疑点与反证,不直接给企业定性;
- 商业秘密仅在授权范围内参与研判,范围外只提示存在、不披露内容;
- 乱序到达的事件按到达顺序形成修订版本,此前版本与依据全部保留;
- 人工豁免到期后自动回到复核队列;
- 授标后的交付质量作为新事件持续校正本次研判。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Callable


class Clearance(Enum):
    """资料密级,研判输出按授权范围逐级放行。"""

    PUBLIC = 1
    INTERNAL = 2
    TRADE_SECRET = 3

    @classmethod
    def of(cls, raw: str | int | None) -> "Clearance":
        if raw is None:
            return cls.PUBLIC
        if isinstance(raw, int):
            return cls(raw)
        return cls[raw.upper()]


def _parse_dt(value: str | datetime) -> datetime:
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(value)


@dataclass(frozen=True)
class Fact:
    """一条可归并的事实(招标规则、报价版本、检验结果等)。

    occurred_at 是事件发生时间,received_at 是进入研判系统的时间;
    两者允许倒挂,用于处理价格执法、质量监管等乱序事件。
    """

    fact_id: str
    category: str
    occurred_at: datetime
    payload: dict[str, Any]
    source: str = ""
    clearance: Clearance = Clearance.PUBLIC
    authorized: bool = True
    supersedes: tuple[str, ...] = ()
    received_at: datetime | None = None

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Fact":
        return cls(
            fact_id=raw["fact_id"],
            category=raw["category"],
            occurred_at=_parse_dt(raw["occurred_at"]),
            payload=raw.get("payload", {}),
            source=raw.get("source", ""),
            clearance=Clearance.of(raw.get("clearance")),
            authorized=raw.get("authorized", True),
            supersedes=tuple(raw.get("supersedes", ())),
            received_at=(
                _parse_dt(raw["received_at"]) if raw.get("received_at") else None
            ),
        )


@dataclass(frozen=True)
class Evidence:
    fact_id: str
    reason: str


@dataclass(frozen=True)
class MissingEvidence:
    requirement: str
    why: str


@dataclass(frozen=True)
class CandidateView:
    """候选解释:只呈现依据、反证与缺口,不给企业定性。"""

    key: str
    label: str
    standing: str
    supports: tuple[Evidence, ...]
    contradicts: tuple[Evidence, ...]
    missing: tuple[MissingEvidence, ...]


@dataclass(frozen=True)
class QueueEntry:
    bidder: str
    reasons: tuple[str, ...]
    exempt_until: datetime | None


@dataclass(frozen=True)
class Calibration:
    """授标后交付质量对上一版研判的校正。"""

    strengthened: tuple[str, ...]
    weakened: tuple[str, ...]
    new_findings: tuple[str, ...]


@dataclass(frozen=True)
class Snapshot:
    """一次研判的不可变修订版本。"""

    revision: int
    generated_at: datetime
    facts_considered: tuple[str, ...]
    superseded_facts: tuple[str, ...]
    excluded_secret_count: int
    candidates: tuple[CandidateView, ...]
    queue: tuple[QueueEntry, ...]
    exempted: tuple[QueueEntry, ...]
    calibration: Calibration | None

    def to_dict(self) -> dict[str, Any]:
        def dt(value: datetime | None) -> str | None:
            return value.isoformat() if value else None

        return {
            "revision": self.revision,
            "generated_at": dt(self.generated_at),
            "facts_considered": list(self.facts_considered),
            "superseded_facts": list(self.superseded_facts),
            "excluded_secret_count": self.excluded_secret_count,
            "candidates": [
                {
                    "key": c.key,
                    "label": c.label,
                    "standing": c.standing,
                    "supports": [vars(e) for e in c.supports],
                    "contradicts": [vars(e) for e in c.contradicts],
                    "missing": [vars(m) for m in c.missing],
                }
                for c in self.candidates
            ],
            "queue": [
                {"bidder": q.bidder, "reasons": list(q.reasons), "exempt_until": dt(q.exempt_until)}
                for q in self.queue
            ],
            "exempted": [
                {"bidder": q.bidder, "reasons": list(q.reasons), "exempt_until": dt(q.exempt_until)}
                for q in self.exempted
            ],
            "calibration": (
                {
                    "strengthened": list(self.calibration.strengthened),
                    "weakened": list(self.calibration.weakened),
                    "new_findings": list(self.calibration.new_findings),
                }
                if self.calibration
                else None
            ),
        }


# 候选解释定义顺序即呈现顺序。
CANDIDATE_LABELS = {
    "efficiency": "效率优势(工艺/管理带来的真实成本下降)",
    "promotion": "短期促销(阶段性让利换取市场)",
    "unsustainable": "不可持续压价(低于可验证成本且无合理来源)",
    "substitution": "交付偷换材料风险",
    "collusion": "关联企业协同投标风险",
}

# 研判结论均为临时性状态,不输出定性结论。
STANDING_WITH_CONTRADICTION = "存在反证,暂不成立"
STANDING_SUPPORTED_GAPS = "有初步支撑,尚缺关键证据"
STANDING_WEAK = "证据不足,仅作备选"
STANDING_OPEN = "疑点待查"


class AssessmentEngine:
    """累积事件并按需生成研判修订版本。"""

    def __init__(self, tender_id: str) -> None:
        self.tender_id = tender_id
        self._facts: dict[str, Fact] = {}
        self._receive_order: list[str] = []
        self._snapshots: list[Snapshot] = []

    def add_fact(self, raw: dict[str, Any] | Fact) -> Fact:
        fact = raw if isinstance(raw, Fact) else Fact.from_dict(raw)
        if fact.fact_id in self._facts:
            raise ValueError(f"事实编号重复: {fact.fact_id}")
        for old_id in fact.supersedes:
            if old_id not in self._facts:
                raise ValueError(f"事实 {fact.fact_id} 声明取代了不存在的 {old_id}")
        self._facts[fact.fact_id] = fact
        self._receive_order.append(fact.fact_id)
        return fact

    @property
    def revisions(self) -> tuple[Snapshot, ...]:
        return tuple(self._snapshots)

    def assess(
        self,
        now: str | datetime,
        viewer_clearance: Clearance | str = Clearance.PUBLIC,
    ) -> Snapshot:
        """基于截至当前已到达的事实生成一个新版本。

        再次调用即产生新修订版;旧版本保留在 revisions 中,不被覆盖。
        """
        moment = _parse_dt(now)
        if isinstance(viewer_clearance, str):
            viewer_clearance = Clearance.of(viewer_clearance)

        ordered = [self._facts[fid] for fid in self._receive_order]
        # 乱序事件:只纳入在研判时点之前已到达(有接收时间按接收时间,否则按发生时间)的事实。
        arrived = [
            f for f in ordered if (f.received_at or f.occurred_at) <= moment
        ]
        superseded_ids = {old for f in arrived for old in f.supersedes}

        visible = [
            f
            for f in arrived
            if f.clearance.value <= viewer_clearance.value and f.authorized
        ]
        excluded_secret = len(arrived) - len(visible)
        active = [f for f in visible if f.fact_id not in superseded_ids]

        bidders = sorted({
            p.get("bidder")
            for f in active
            for p in [f.payload]
            if f.category in {"bid_version", "low_bid_signal"} and p.get("bidder")
        })
        # 以最新报价版本确定每个投标人的有效报价。
        latest_bid = self._latest_bids(active)

        candidates = tuple(
            view
            for bidder in bidders
            for view in (
                self._view_candidate("efficiency", bidder, active, latest_bid),
                self._view_candidate("promotion", bidder, active, latest_bid),
                self._view_candidate("unsustainable", bidder, active, latest_bid),
                self._view_candidate("substitution", bidder, active, latest_bid),
                self._view_candidate("collusion", bidder, active, latest_bid),
            )
        )

        queue, exempted = self._build_queue(bidders, active, candidates, moment)
        calibration = self._calibrate(candidates, active, moment)

        snapshot = Snapshot(
            revision=len(self._snapshots) + 1,
            generated_at=moment,
            facts_considered=tuple(f.fact_id for f in active),
            superseded_facts=tuple(
                fid for fid in self._receive_order if fid in superseded_ids
            ),
            excluded_secret_count=excluded_secret,
            candidates=candidates,
            queue=queue,
            exempted=exempted,
            calibration=calibration,
        )
        self._snapshots.append(snapshot)
        return snapshot

    # ------------------------------------------------------------------
    # 报价版本
    # ------------------------------------------------------------------
    @staticmethod
    def _latest_bids(facts: list[Fact]) -> dict[str, dict[str, Any]]:
        bids: dict[str, dict[str, Any]] = {}
        for f in facts:
            if f.category != "bid_version":
                continue
            bidder = f.payload.get("bidder")
            version = f.payload.get("version", 1)
            old = bids.get(bidder)
            if old is None or version >= old.get("version", 1):
                merged = dict(f.payload)
                merged["fact_id"] = f.fact_id
                bids[bidder] = merged
        return bids

    # ------------------------------------------------------------------
    # 候选解释规则:每条规则返回 (支撑, 反证, 缺口)
    # ------------------------------------------------------------------
    def _view_candidate(
        self,
        key: str,
        bidder: str,
        facts: list[Fact],
        latest_bid: dict[str, dict[str, Any]],
    ) -> CandidateView:
        rules: dict[str, Callable[..., tuple[list[Evidence], list[Evidence], list[MissingEvidence]]]] = {
            "efficiency": self._rule_efficiency,
            "promotion": self._rule_promotion,
            "unsustainable": self._rule_unsustainable,
            "substitution": self._rule_substitution,
            "collusion": self._rule_collusion,
        }
        supports, contradicts, missing = rules[key](bidder, facts, latest_bid)
        if contradicts:
            standing = STANDING_WITH_CONTRADICTION
        elif supports and missing:
            standing = STANDING_SUPPORTED_GAPS
        elif supports:
            standing = STANDING_OPEN
        else:
            standing = STANDING_WEAK
        return CandidateView(
            key=f"{bidder}:{key}",
            label=CANDIDATE_LABELS[key],
            standing=standing,
            supports=tuple(supports),
            contradicts=tuple(contradicts),
            missing=tuple(missing),
        )

    @staticmethod
    def _mine(facts: list[Fact], bidder: str, *categories: str) -> list[Fact]:
        return [
            f
            for f in facts
            if f.category in categories and f.payload.get("bidder") == bidder
        ]

    @staticmethod
    def _below_cost(latest_bid: dict[str, Any], facts: list[Fact], bidder: str) -> tuple[bool, float | None, float | None]:
        bid = latest_bid.get(bidder)
        if not bid:
            return False, None, None
        price = bid.get("unit_price")
        floors = [
            f.payload.get("material_cost_floor")
            for f in facts
            if f.category == "verifiable_cost"
            and f.payload.get("bidder") == bidder
            and f.payload.get("material_cost_floor") is not None
        ]
        if price is None or not floors:
            return False, price, (min(floors) if floors else None)
        floor = min(floors)
        return price < floor, price, floor

    def _rule_efficiency(self, bidder, facts, latest_bid):
        supports: list[Evidence] = []
        contradicts: list[Evidence] = []
        missing: list[MissingEvidence] = []

        below, _, _ = self._below_cost(latest_bid, facts, bidder)
        if not below:
            return supports, contradicts, missing

        for f in self._mine(facts, bidder, "efficiency_evidence"):
            if f.payload.get("verified"):
                supports.append(Evidence(f.fact_id, f.payload.get("note", "经核验的工艺/管理效率证据")))
            else:
                missing.append(MissingEvidence(
                    f.payload.get("note", "效率主张待第三方核验"),
                    "企业自述效率不等于可验证成本,需要独立核验",
                ))
        for f in self._mine(facts, bidder, "capacity"):
            if not f.payload.get("shortfall"):
                supports.append(Evidence(f.fact_id, "产能可覆盖本批次交付"))
            else:
                contradicts.append(Evidence(f.fact_id, "产能不足以支撑其效率交付主张"))
        for f in self._mine(facts, bidder, "delivery_history"):
            if f.payload.get("on_time_rate", 1) >= 0.95 and not f.payload.get("quality_finding"):
                supports.append(Evidence(f.fact_id, "历史交付按时保质"))
        for f in self._mine(facts, bidder, "inspection"):
            if f.payload.get("result") == "pass":
                supports.append(Evidence(f.fact_id, f.payload.get("note", "检验合格")))
            elif f.payload.get("result") == "fail":
                contradicts.append(Evidence(f.fact_id, f.payload.get("note", "检验不合格,效率优势不能解释质量缺陷")))

        cost = self._mine(facts, bidder, "verifiable_cost")
        if not any(f.payload.get("breakdown_verified") for f in cost):
            missing.append(MissingEvidence(
                "可验证成本构成(主材、能耗、人工)逐项核验",
                "需确认低价来自成本下降而非省略主材",
            ))
        if not self._mine(facts, bidder, "efficiency_evidence"):
            missing.append(MissingEvidence(
                "工艺或管理效率的客观证据(产线良率、专利工艺等)",
                "低于主材成本的报价必须解释成本下降来源",
            ))
        return supports, contradicts, missing

    def _rule_promotion(self, bidder, facts, latest_bid):
        supports: list[Evidence] = []
        contradicts: list[Evidence] = []
        missing: list[MissingEvidence] = []

        below, _, _ = self._below_cost(latest_bid, facts, bidder)
        if not below:
            return supports, contradicts, missing

        for f in self._mine(facts, bidder, "promotion_evidence"):
            supports.append(Evidence(f.fact_id, f.payload.get("note", "企业声明阶段性促销/市场进入让价")))
            if f.payload.get("subsidy_proof"):
                supports.append(Evidence(f.fact_id, "提供补贴或专项预算来源证明"))
            else:
                missing.append(MissingEvidence(
                    "促销补贴来源证明",
                    "让价需有可持续的资金来源,否则与不可持续压价无法区分",
                ))
            if not f.payload.get("scope_within_contract"):
                contradicts.append(Evidence(
                    f.fact_id,
                    "促销覆盖范围/期限超过其声明的限量,不构成短期促销",
                ))
        for f in self._mine(facts, bidder, "delivery_history"):
            if f.payload.get("serial_below_cost_bids"):
                contradicts.append(Evidence(f.fact_id, "多次在多个项目以低于成本价中标,非短期行为"))
        if not self._mine(facts, bidder, "promotion_evidence"):
            missing.append(MissingEvidence(
                "促销声明及限量、限期承诺",
                "没有促销声明时该解释无来源",
            ))
        return supports, contradicts, missing

    def _rule_unsustainable(self, bidder, facts, latest_bid):
        supports: list[Evidence] = []
        contradicts: list[Evidence] = []
        missing: list[MissingEvidence] = []

        below, price, floor = self._below_cost(latest_bid, facts, bidder)
        if below:
            supports.append(Evidence(
                latest_bid[bidder]["fact_id"],
                f"报价 {price} 低于可验证主材成本下限 {floor}",
            ))
        else:
            return supports, contradicts, missing

        if not self._mine(facts, bidder, "efficiency_evidence") and not self._mine(
            facts, bidder, "promotion_evidence"
        ):
            supports.append(Evidence(latest_bid[bidder]["fact_id"], "报价方未提供效率或促销方面的解释"))

        for f in self._mine(facts, bidder, "capacity"):
            if f.payload.get("shortfall"):
                supports.append(Evidence(f.fact_id, "产能存在缺口,低价合同难以自身履约"))
        for f in facts:
            if f.category == "material_price" and f.payload.get("change_pct", 0) >= 0.05:
                supports.append(Evidence(
                    f.fact_id,
                    f"投标后主材价格上涨 {f.payload['change_pct']:.0%},未锁价则报价更不可持续",
                ))
        for f in self._mine(facts, bidder, "verifiable_cost"):
            if f.payload.get("breakdown_verified"):
                contradicts.append(Evidence(f.fact_id, "成本构成已经第三方核验,压价不可持续的判断需重新评估"))
        for f in self._mine(facts, bidder, "hedging"):
            if f.payload.get("locked_price"):
                contradicts.append(Evidence(f.fact_id, "已提供主材锁价/长协证据"))
        missing.append(MissingEvidence(
            "履约担保与不降低标准的书面承诺",
            "用于约束低于成本中标后的履约行为",
        ))
        return supports, contradicts, missing

    def _rule_substitution(self, bidder, facts, latest_bid):
        supports: list[Evidence] = []
        contradicts: list[Evidence] = []
        missing: list[MissingEvidence] = []

        below, _, _ = self._below_cost(latest_bid, facts, bidder)
        if not below:
            return supports, contradicts, missing

        for f in self._mine(facts, bidder, "delivery_history", "quality_regulator", "price_enforcement"):
            if f.payload.get("substitution_finding"):
                supports.append(Evidence(f.fact_id, f.payload.get("note", "历史/监管存在偷换材料认定记录")))
        for f in self._mine(facts, bidder, "complaint"):
            status = f.payload.get("status")
            if status == "upheld":
                supports.append(Evidence(f.fact_id, f.payload.get("note", "关于材料以次充好的投诉经查实")))
            elif status == "dismissed":
                contradicts.append(Evidence(f.fact_id, "同类投诉经核查不成立"))
        for f in self._mine(facts, bidder, "inspection"):
            if f.payload.get("result") == "fail" and f.payload.get("material_mismatch"):
                supports.append(Evidence(f.fact_id, "进场材料与封样/承诺规格不符"))
            elif f.payload.get("result") == "pass" and f.payload.get("material_verified"):
                contradicts.append(Evidence(f.fact_id, "进场材料检验与承诺规格一致"))
        bid = latest_bid.get(bidder, {})
        if not bid.get("brand_locked"):
            missing.append(MissingEvidence(
                "主要材料品牌、规格的封样与锁定承诺",
                "报价未锁定材料品牌规格时,交付环节存在替换空间",
            ))
        missing.append(MissingEvidence(
            "交付批次进场检验与封样对比结果",
            "该疑点只能由交付环节的检验结果排除或证实",
        ))
        return supports, contradicts, missing

    def _rule_collusion(self, bidder, facts, latest_bid):
        supports: list[Evidence] = []
        contradicts: list[Evidence] = []
        missing: list[MissingEvidence] = []

        bid = latest_bid.get(bidder)
        if not bid:
            return supports, contradicts, missing

        partners = set()
        for f in facts:
            if f.category == "affiliate" and bidder in f.payload.get("entities", []):
                others = [e for e in f.payload["entities"] if e != bidder]
                partners.update(others)
                supports.append(Evidence(
                    f.fact_id,
                    f.payload.get("note", f"与 {','.join(others)} 存在关联关系"),
                ))
            if f.category == "joint_bid" and bidder in f.payload.get("members", []):
                supports.append(Evidence(f.fact_id, f.payload.get("note", "存在联合投标情形,需核验是否构成实体竞争")))
        if partners:
            also_bidding = [p for p in partners if p in latest_bid]
            if also_bidding:
                supports.append(Evidence(
                    bid["fact_id"],
                    f"关联方 {','.join(also_bidding)} 同时参与本项目报价",
                ))
            missing.append(MissingEvidence(
                "关联企业股权/人员交叉的核查结论与独立报价证明",
                "关联关系本身不违法,需要区分合法联合与协同报价",
            ))
        for f in self._mine(facts, bidder, "complaint"):
            if f.payload.get("topic") == "collusion" and f.payload.get("status") == "dismissed":
                contradicts.append(Evidence(f.fact_id, "围标投诉经查证不成立"))
        return supports, contradicts, missing

    # ------------------------------------------------------------------
    # 复核队列与豁免
    # ------------------------------------------------------------------
    @staticmethod
    def _active_exemption(facts: list[Fact], bidder: str, moment: datetime) -> Fact | None:
        exemptions = [
            f
            for f in facts
            if f.category == "exemption"
            and f.payload.get("bidder") == bidder
        ]
        active = [
            f
            for f in exemptions
            if _parse_dt(f.payload["expires_at"]) > moment
        ]
        return max(active, key=lambda f: _parse_dt(f.payload["expires_at"]), default=None)

    def _build_queue(self, bidders, facts, candidates, moment):
        queue: list[QueueEntry] = []
        exempted: list[QueueEntry] = []
        for bidder in bidders:
            views = [c for c in candidates if c.key.startswith(f"{bidder}:")]
            reasons: list[str] = []
            for c in views:
                if c.standing == STANDING_WITH_CONTRADICTION:
                    continue
                for m in c.missing:
                    reasons.append(f"[{c.label}] 缺证: {m.requirement}")
                if c.standing == STANDING_OPEN and c.supports:
                    reasons.append(f"[{c.label}] 疑点已现,需复核结论")
            # 到期豁免本身也是回队理由。
            expired = [
                f
                for f in facts
                if f.category == "exemption"
                and f.payload.get("bidder") == bidder
                and _parse_dt(f.payload["expires_at"]) <= moment
            ]
            for f in expired:
                reasons.append(
                    f"人工豁免已于 {f.payload['expires_at']} 到期,自动回到复核队列"
                )
            if not reasons:
                continue
            active = self._active_exemption(facts, bidder, moment)
            if active is not None:
                exempted.append(QueueEntry(
                    bidder,
                    tuple(reasons),
                    _parse_dt(active.payload["expires_at"]),
                ))
            else:
                queue.append(QueueEntry(bidder, tuple(reasons), None))
        return tuple(queue), tuple(exempted)

    # ------------------------------------------------------------------
    # 交付质量校正
    # ------------------------------------------------------------------
    def _calibrate(self, candidates, facts, moment):
        # 只取上一版研判之后到达的交付反馈,体现“后续交付继续校正本次研判”。
        if self._snapshots:
            last_at = self._snapshots[-1].generated_at
            feedback = [
                f
                for f in facts
                if f.category == "delivery_feedback" and f.occurred_at > last_at
            ]
        else:
            feedback = [f for f in facts if f.category == "delivery_feedback"]
        if not feedback:
            return None

        strengthened: list[str] = []
        weakened: list[str] = []
        new_findings: list[str] = []
        for f in feedback:
            bidder = f.payload.get("bidder")
            result = f.payload.get("result")
            if result == "fail" and f.payload.get("material_mismatch"):
                strengthened.append(f"{bidder}:substitution")
                new_findings.append(Evidence(f.fact_id, f.payload.get("note", "交付检验发现材料不符")).reason)
            elif result == "pass":
                weakened.append(f"{bidder}:substitution")
                weakened.append(f"{bidder}:unsustainable")
                new_findings.append(Evidence(f.fact_id, f.payload.get("note", "交付检验合格")).reason)
        return Calibration(
            strengthened=tuple(dict.fromkeys(strengthened)),
            weakened=tuple(dict.fromkeys(weakened)),
            new_findings=tuple(new_findings),
        )


def load_case(path: str | Path) -> AssessmentEngine:
    """从案件资料文件构造引擎,资料格式见 fixtures/assessment.json。"""
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if raw.get("domain") != "low-bid-quality-warning":
        raise ValueError("案件资料领域标识不匹配")
    engine = AssessmentEngine(raw["tender_id"])
    for item in raw.get("facts", []):
        engine.add_fact(item)
    return engine
