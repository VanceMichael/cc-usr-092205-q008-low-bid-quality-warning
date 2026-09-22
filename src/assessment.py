"""低价投标风险研判引擎。

设计原则：
- 事实只追加、不覆盖。事件带“发生时间/接收时间”，迟到的监管线索按接收
  时间进入研判，历史快照保留当时的全部依据。
- 输出候选解释（效率优势、短期促销、不可持续压价）及其支持证据、反证与
  尚缺证据，不输出单一分数，也不对企业作违规定性。
- 商业秘密事件仅对授权角色可见；未授权角色得到的是“该材料存在但不可见”。
- 人工豁免只改变队列状态，不删除疑点；豁免到期（或观察期内出现不合格
  交付）自动回到复核队列。
- 交付与监管结果持续进入证据流，对既有解释作增强或削弱的校正。
"""

from __future__ import annotations

import copy
from datetime import date
from itertools import count

# 候选解释：键 -> 展示名
HYPOTHESES = {
    "efficiency_advantage": "效率优势：工艺或管理效率带来真实成本下降",
    "short_term_promotion": "短期促销：以锁价、让利等阶段性安排换取订单",
    "unsustainable_undercutting": "不可持续压价：低于成本竞标，履约中存在降标风险",
}

_QUALITY_PASS = 0.98
_ONTIME_PASS = 0.95
_PRICE_SHOCK_PCT = 8.0


def _d(value: str) -> date:
    return date.fromisoformat(value)


def _point(event_id: str, text: str) -> dict:
    return {"event": event_id, "text": text}


class EvidenceLog:
    """只追加的证据日志。"""

    def __init__(self, events: list[dict]):
        self._entries: list[dict] = []
        seen = set()
        for event in events:
            if event["id"] in seen:
                raise ValueError(f"事件编号重复：{event['id']}")
            seen.add(event["id"])
            self._entries.append(copy.deepcopy(event))

    def visible(self, role: str, as_of: str) -> list[dict]:
        """截至 as_of 已接收、且该角色有权查看的事件，按接收时间排序。"""
        result = []
        for event in self._entries:
            if event["received_at"] > as_of:
                continue
            if event.get("confidential") and role not in event.get("allowed_roles", []):
                continue
            result.append(copy.deepcopy(event))
        result.sort(key=lambda e: (e["received_at"], e["id"]))
        return result

    def redacted(self, role: str, as_of: str) -> list[dict]:
        """已接收但因授权范围对该角色屏蔽的事件（只暴露编号与类型）。"""
        return [
            {"event": e["id"], "type": e["type"]}
            for e in self._entries
            if e["received_at"] <= as_of
            and e.get("confidential")
            and role not in e.get("allowed_roles", [])
        ]


class AssessmentEngine:
    """基于案件事件流生成不可变研判快照。"""

    def __init__(self, case: dict, viewer_role: str):
        self.case = copy.deepcopy(case)
        self.viewer_role = viewer_role
        self.log = EvidenceLog(self.case["events"])
        self._snapshot_seq = count(1)
        self.history: list[dict] = []

    # ---------- 基础归集 ----------

    def _affiliate_map(self, events: list[dict]) -> dict[str, set[str]]:
        groups: dict[str, set[str]] = {b["id"]: {b["id"]} for b in self.case["bidders"]}
        for e in events:
            if e["type"] != "affiliate":
                continue
            a, b = e["payload"]["bidder"], e["payload"]["affiliate"]
            merged = groups[a] | groups[b]
            for member in merged:
                groups[member] = merged
        return groups

    def _latest_bid(self, events: list[dict], bidder: str) -> dict | None:
        versions = [
            e for e in events
            if e["type"] == "bid_version" and e["payload"]["bidder"] == bidder
        ]
        if not versions:
            return None
        return max(versions, key=lambda e: (e["payload"]["version"], e["occurred_at"]))

    def _reference_price(self, events: list[dict], on_or_before: str) -> float | None:
        changes = [
            e for e in events
            if e["type"] == "material_price_change" and e["occurred_at"] <= on_or_before
        ]
        if not changes:
            return None
        return max(changes, key=lambda e: e["occurred_at"])["payload"]["reference_price"]

    def _joint_bids(self, events: list[dict]) -> list[dict]:
        return [e for e in events if e["type"] == "joint_bid"]

    # ---------- 队列状态 ----------

    def _queue_status(self, events: list[dict], bidder: str, as_of: str) -> dict:
        waivers = [
            e for e in events
            if e["type"] == "waiver"
            and e["payload"]["bidder"] == bidder
            and e["occurred_at"] <= as_of
        ]
        fails = [
            e for e in events
            if e["type"] == "delivery_outcome"
            and e["payload"]["bidder"] == bidder
            and e["payload"]["result"] == "fail"
        ]
        if waivers:
            waiver = max(waivers, key=lambda e: e["occurred_at"])
            expires = waiver["payload"]["expires_at"]
            latest_fail = max((f["occurred_at"] for f in fails), default=None)
            if latest_fail and latest_fail >= waiver["occurred_at"]:
                return {
                    "status": "观察期内出现不合格交付，提前回到复核队列",
                    "waiver_event": waiver["id"],
                    "expires_at": expires,
                }
            if as_of <= expires:
                return {
                    "status": f"人工豁免观察期，至 {expires} 自动回到复核队列",
                    "waiver_event": waiver["id"],
                    "expires_at": expires,
                }
            return {
                "status": "人工豁免已到期，自动回到复核队列",
                "waiver_event": waiver["id"],
                "expires_at": expires,
            }
        return {"status": "待复核", "waiver_event": None, "expires_at": None}

    # ---------- 解释评估 ----------

    def _empty_hypotheses(self) -> dict[str, dict]:
        return {
            key: {"label": label, "supporting": [], "counter": [], "missing": []}
            for key, label in HYPOTHESES.items()
        }

    def _evaluate(
        self, events: list[dict], bidder: str, as_of: str, affiliates: set[str]
    ) -> dict[str, dict]:
        h = self._empty_hypotheses()
        rule = next((e for e in events if e["type"] == "tender_rule"), None)
        bid = self._latest_bid(events, bidder)

        # 事实测算（不是评分）：报价与主材成本的差额
        fact = None
        if rule and bid:
            item = rule["payload"]["material_items"][0]
            content = item["required_content"]
            ref_price = self._reference_price(events, bid["occurred_at"])
            if ref_price is not None:
                floor = round(content * ref_price, 2)
                price = bid["payload"]["unit_price"]
                fact = {
                    "bid_event": bid["id"],
                    "unit_price": price,
                    "material": item["code"],
                    "required_content": content,
                    "reference_price": ref_price,
                    "material_floor": floor,
                    "gap": round(price - floor, 2),
                    "note": "主材成本下限=单位主材含量×同期参考价，仅为事实测算，不构成结论",
                }
                if price < floor:
                    h["unsustainable_undercutting"]["supporting"].append(_point(
                        bid["id"],
                        f"最终报价 {price} 低于按同期参考价测算的主材成本下限 {floor}"
                        f"（每件低 {floor - price:.2f}）",
                    ))
                else:
                    h["unsustainable_undercutting"]["counter"].append(_point(
                        bid["id"], f"最终报价 {price} 不低于主材成本下限 {floor}"
                    ))

        # 可验证成本项（锁价协议等，通常为商业秘密）
        for e in events:
            if e["type"] != "verifiable_cost" or e["payload"]["bidder"] != bidder:
                continue
            locked = e["payload"]["locked_unit_price"]
            if rule and bid:
                content = rule["payload"]["material_items"][0]["required_content"]
                locked_floor = round(content * locked, 2)
                price = bid["payload"]["unit_price"]
                if price >= locked_floor:
                    text = (
                        f"锁价协议单价 {locked}，对应主材成本下限 {locked_floor}，"
                        f"报价 {price} 可覆盖（文件授权范围内使用）"
                    )
                    h["short_term_promotion"]["supporting"].append(_point(e["id"], text))
                    h["unsustainable_undercutting"]["counter"].append(_point(e["id"], text))
                else:
                    h["unsustainable_undercutting"]["supporting"].append(_point(
                        e["id"],
                        f"即使按锁价单价 {locked} 测算，下限 {locked_floor} 仍高于报价",
                    ))
            h["short_term_promotion"]["missing"].append(_point(
                e["id"], "锁价数量是否覆盖本批次、锁价之外的让利期限安排"
            ))

        # 产能：自有产能与联合体合并产能分别判断
        for e in events:
            if e["type"] != "capacity" or e["payload"]["bidder"] != bidder:
                continue
            if rule:
                need_per_month = (
                    rule["payload"]["quantity"] / rule["payload"]["delivery_window_months"]
                )
                free = e["payload"]["monthly_capacity"] - e["payload"]["monthly_committed"]
                if free < need_per_month:
                    text = (
                        f"自有月可用产能约 {free:.0f}，低于交付所需约 {need_per_month:.0f}/月"
                    )
                    h["unsustainable_undercutting"]["supporting"].append(_point(e["id"], text))
                else:
                    text = f"自有月可用产能约 {free:.0f}，可满足交付所需 {need_per_month:.0f}/月"
                    h["efficiency_advantage"]["counter"].append(_point(e["id"], text))
                    h["unsustainable_undercutting"]["counter"].append(_point(e["id"], text))

        # 联合体：成员产能并入，迟到的联合体协议会更新产能判断
        for jb in self._joint_bids(events):
            if bidder not in (jb["payload"]["leaders"] + jb["payload"]["members"]):
                continue
            member_cap = sum(jb["payload"].get("member_capacity", {}).values())
            h["unsustainable_undercutting"]["counter"].append(_point(
                jb["id"], f"联合体成员可计入月产能 {member_cap}，需核验分工与协议"
            ))
            h["efficiency_advantage"]["missing"].append(_point(
                jb["id"], "联合体分工协议及成员实际开工率的核验结果"
            ))

        # 历史交付
        for e in events:
            if e["type"] != "delivery_history" or e["payload"]["bidder"] != bidder:
                continue
            p = e["payload"]
            if p["quality_pass_rate"] >= _QUALITY_PASS and p["on_time_rate"] >= _ONTIME_PASS:
                text = (
                    f"{p['period']}准时率 {p['on_time_rate']:.0%}、"
                    f"质量合格率 {p['quality_pass_rate']:.0%}"
                )
                h["efficiency_advantage"]["supporting"].append(_point(e["id"], text))
                h["unsustainable_undercutting"]["counter"].append(_point(e["id"], text))
            else:
                h["unsustainable_undercutting"]["supporting"].append(_point(
                    e["id"], "历史准时率或合格率偏低，需关注履约稳定性"
                ))

        # 投标留样检验
        for e in events:
            if e["type"] != "inspection" or e["payload"]["bidder"] != bidder:
                continue
            if e["payload"]["result"] == "pass":
                h["unsustainable_undercutting"]["counter"].append(_point(
                    e["id"], "投标留样检验合格：" + "、".join(e["payload"]["items"])
                ))
            else:
                h["unsustainable_undercutting"]["supporting"].append(_point(
                    e["id"], "投标留样检验不合格"
                ))

        # 投诉：核查中只形成疑点，结论缺失
        for e in events:
            if e["type"] != "complaint" or e["payload"]["bidder"] != bidder:
                continue
            p = e["payload"]
            if p["status"] == "核查中":
                h["unsustainable_undercutting"]["supporting"].append(_point(
                    e["id"], f"存在未结投诉：{p['allegation']}（{p['status']}）"
                ))
                h["unsustainable_undercutting"]["missing"].append(_point(
                    e["id"], f"投诉 {p.get('case_ref', '')} 的核查结论"
                ))
            elif p["status"] == "成立":
                h["unsustainable_undercutting"]["supporting"].append(_point(
                    e["id"], f"投诉经查证成立：{p['allegation']}"
                ))
            else:
                h["unsustainable_undercutting"]["counter"].append(_point(
                    e["id"], f"投诉经查不成立：{p['allegation']}"
                ))

        # 关联企业的执法与监管记录：归并为关联风险，不等于本主体结论
        related = affiliates - {bidder}
        has_related_record = False
        for e in events:
            if e["type"] not in {"price_enforcement", "quality_regulation"}:
                continue
            subject = e["payload"].get("subject")
            if subject not in related:
                continue
            has_related_record = True
            if e["type"] == "price_enforcement":
                h["unsustainable_undercutting"]["supporting"].append(_point(
                    e["id"],
                    f"关联企业 {subject} 曾因低于成本报价被{ e['payload']['penalty']}"
                    "（关联记录，不视为本主体行为）",
                ))
            else:
                h["unsustainable_undercutting"]["supporting"].append(_point(
                    e["id"],
                    f"关联企业 {subject} 曾因低标号材料替代被{ e['payload']['penalty']}"
                    "（关联记录，不视为本主体行为）",
                ))
        if has_related_record:
            h["unsustainable_undercutting"]["missing"].append(_point(
                "system", "关联企业记录与本主体在采购、生产上的实际隔离情况"
            ))

        # 申诉补证：未经核验的主张列为反证候选与待补证据，不直接采信
        for e in events:
            if e["type"] != "appeal_supplement" or e["payload"]["bidder"] != bidder:
                continue
            for item in e["payload"]["items"]:
                if item.get("verified"):
                    h["unsustainable_undercutting"]["counter"].append(_point(
                        e["id"], f"申诉材料已经核验：{item['detail']}"
                    ))
                else:
                    h["unsustainable_undercutting"]["missing"].append(_point(
                        e["id"], f"申诉材料待核验：{item['detail']}"
                    ))

        # 原料价格突变：以报价时点为界，评估履约期成本压力
        if fact is not None:
            shocks = [
                e for e in events
                if e["type"] == "material_price_change"
                and e["occurred_at"] > (bid["occurred_at"] if bid else as_of)
                and abs(e["payload"].get("change_pct", 0)) >= _PRICE_SHOCK_PCT
            ]
            for e in shocks:
                p = e["payload"]
                if p["change_pct"] > 0:
                    latest_floor = round(fact["required_content"] * p["reference_price"], 2)
                    h["unsustainable_undercutting"]["supporting"].append(_point(
                        e["id"],
                        f"报价后原料涨价 {p['change_pct']}%，主材成本下限升至 {latest_floor}，"
                        "履约期降标压力增大（仍需核验供货合同覆盖情况）",
                    ))
                else:
                    h["unsustainable_undercutting"]["counter"].append(_point(
                        e["id"], f"报价后原料降价 {abs(p['change_pct'])}%，成本压力缓解"
                    ))

        # 交付结果：持续校正既有解释，但不替代调查定性
        outcomes = [
            e for e in events
            if e["type"] == "delivery_outcome" and e["payload"]["bidder"] == bidder
        ]
        for e in outcomes:
            p = e["payload"]
            if p["result"] == "fail":
                h["unsustainable_undercutting"]["supporting"].append(_point(
                    e["id"],
                    f"{p['batch']}交付检验不合格：{p['finding']}"
                    "（与不可持续压价解释方向一致，原因仍须调查认定）",
                ))
                h["efficiency_advantage"]["counter"].append(_point(
                    e["id"], f"{p['batch']}交付不合格，效率优势解释被削弱"
                ))
                h["unsustainable_undercutting"]["missing"].append(_point(
                    e["id"], "不合格批次的原因调查、责任认定与整改验证结论"
                ))
            else:
                h["unsustainable_undercutting"]["counter"].append(_point(
                    e["id"], f"{p['batch']}交付检验合格：{p['finding']}"
                ))

        # 效率优势解释固定需要的补证方向
        h["efficiency_advantage"]["missing"].append(_point(
            "system", "省材工艺的第三方核验结论与实际成材率/损耗率生产记录"
        ))

        return h

    def _standing(self, hyp: dict, outcomes: list[dict]) -> str:
        has_s, has_c = bool(hyp["supporting"]), bool(hyp["counter"])
        if has_s and has_c:
            base = "正反证据并存"
        elif has_s:
            base = "有支持性证据，反证不足"
        elif has_c:
            base = "暂无支持性证据，存在反证"
        else:
            base = "证据不足"
        latest_fail = max(
            (e["occurred_at"] for e in outcomes if e["payload"]["result"] == "fail"),
            default=None,
        )
        latest_pass = max(
            (e["occurred_at"] for e in outcomes if e["payload"]["result"] == "pass"),
            default=None,
        )
        if latest_fail and (latest_pass is None or latest_fail > latest_pass):
            return base + "；后续交付出现同向不合格结果"
        if latest_pass and latest_fail and latest_pass > latest_fail:
            return base + "；整改后交付合格，信号减弱"
        return base

    # ---------- 快照 ----------

    def assess(self, bidder_id: str, as_of: str) -> dict:
        """生成截至 as_of 的研判快照并留存历史。"""
        if not any(b["id"] == bidder_id for b in self.case["bidders"]):
            raise ValueError(f"投标人不存在：{bidder_id}")

        events = self.log.visible(self.viewer_role, as_of)
        redacted = self.log.redacted(self.viewer_role, as_of)
        affiliate_map = self._affiliate_map(events)
        affiliates = affiliate_map.get(bidder_id, {bidder_id})

        bid = self._latest_bid(events, bidder_id)
        fact = None
        if bid:
            rule = next((e for e in events if e["type"] == "tender_rule"), None)
            if rule:
                item = rule["payload"]["material_items"][0]
                ref_price = self._reference_price(events, bid["occurred_at"])
                if ref_price is not None:
                    floor = round(item["required_content"] * ref_price, 2)
                    fact = {
                        "bid_event": bid["id"],
                        "unit_price": bid["payload"]["unit_price"],
                        "material_floor": floor,
                        "gap": round(bid["payload"]["unit_price"] - floor, 2),
                    }

        hypotheses = self._evaluate(events, bidder_id, as_of, affiliates)
        outcomes = [
            e for e in events
            if e["type"] == "delivery_outcome" and e["payload"]["bidder"] == bidder_id
        ]
        for key, hyp in hypotheses.items():
            hyp["standing"] = self._standing(hyp, outcomes)

        missing = []
        seen = set()
        for hyp in hypotheses.values():
            for item in hyp["missing"]:
                marker = (item["event"], item["text"])
                if marker not in seen:
                    seen.add(marker)
                    missing.append(item)

        snapshot = {
            "snapshot_id": f"S{next(self._snapshot_seq)}",
            "case_id": self.case["case_id"],
            "subject_bidder": bidder_id,
            "viewer_role": self.viewer_role,
            "as_of": as_of,
            "based_on": [e["id"] for e in events],
            "redacted_for_role": redacted,
            "price_fact": fact,
            "affiliates": sorted(affiliates - {bidder_id}),
            "queue": self._queue_status(events, bidder_id, as_of),
            "hypotheses": hypotheses,
            "open_questions": missing,
            "disclaimer": "本快照只归集疑点、反证与待补证据，不对任何主体作违规定性；后续证据将生成新快照，本快照依据保留不变。",
        }
        stored = copy.deepcopy(snapshot)
        if self.history:
            stored["prior_snapshot"] = self.history[-1]["snapshot_id"]
            snapshot["prior_snapshot"] = stored["prior_snapshot"]
        self.history.append(stored)
        return snapshot

    def lowest_bidder(self, as_of: str) -> str | None:
        """截至 as_of 按各自最终报价找出最低报价人。"""
        events = self.log.visible(self.viewer_role, as_of)
        latest = {}
        for e in events:
            if e["type"] == "bid_version":
                bidder = e["payload"]["bidder"]
                old = latest.get(bidder)
                if old is None or e["payload"]["version"] > old["payload"]["version"]:
                    latest[bidder] = e
        if not latest:
            return None
        return min(latest.values(), key=lambda e: e["payload"]["unit_price"])["payload"]["bidder"]
