"""读取并校验案件事件流资料。

资料是只追加的事件流：事实以事件形式进入，事件带有“发生时间”和
“接收时间”，监管线索迟到归集时两者可以不一致。校验只检查结构与
引用完整性，不对事件内容作事实判断。
"""

import json
from datetime import date
from pathlib import Path

REQUIRED_FIELDS = {"domain", "version", "case_id", "subject", "bidders", "events"}

EVENT_TYPES = {
    "tender_rule",
    "bid_version",
    "verifiable_cost",
    "capacity",
    "delivery_history",
    "inspection",
    "complaint",
    "affiliate",
    "joint_bid",
    "material_price_change",
    "appeal_supplement",
    "price_enforcement",
    "quality_regulation",
    "waiver",
    "delivery_outcome",
}

# payload 中按投标人编号引用主体的事件
BIDDER_REF_TYPES = {
    "bid_version",
    "verifiable_cost",
    "capacity",
    "delivery_history",
    "inspection",
    "complaint",
    "affiliate",
    "waiver",
    "delivery_outcome",
    "price_enforcement",
    "quality_regulation",
}


def _parse_date(value: str, where: str) -> str:
    try:
        date.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{where} 日期格式应为 YYYY-MM-DD：{value!r}") from exc
    return value


def load_case(path: Path) -> dict:
    """读取结构完整、引用一致的案件事件流。"""
    value = json.loads(path.read_text(encoding="utf-8"))

    missing = REQUIRED_FIELDS - value.keys()
    if missing:
        raise ValueError(f"案件资料缺少必要字段：{sorted(missing)}")
    if value["domain"] != "low-bid-quality-warning":
        raise ValueError("案件资料领域标识不匹配")
    if value["version"] < 1:
        raise ValueError("案件资料版本号无效")
    if len(value["bidders"]) < 2:
        raise ValueError("案件至少需要两名投标人")

    bidder_ids = {b["id"] for b in value["bidders"]}
    if len(bidder_ids) != len(value["bidders"]):
        raise ValueError("投标人编号重复")

    event_ids = set()
    for event in value["events"]:
        eid = event.get("id")
        if not eid or eid in event_ids:
            raise ValueError(f"事件编号缺失或重复：{eid!r}")
        event_ids.add(eid)

        etype = event.get("type")
        if etype not in EVENT_TYPES:
            raise ValueError(f"事件 {eid} 类型未知：{etype!r}")

        _parse_date(event["occurred_at"], f"事件 {eid}")
        _parse_date(event["received_at"], f"事件 {eid}")
        if event["occurred_at"] > event["received_at"]:
            # 允许，但发生时间不应晚于接收时间
            raise ValueError(f"事件 {eid} 发生时间晚于接收时间")

        payload = event.get("payload")
        if not isinstance(payload, dict):
            raise ValueError(f"事件 {eid} 缺少 payload")

        if etype in BIDDER_REF_TYPES:
            ref = payload.get("bidder") or payload.get("subject")
            if ref not in bidder_ids:
                raise ValueError(f"事件 {eid} 引用了不存在的主体：{ref!r}")

        if etype == "affiliate" and payload.get("affiliate") not in bidder_ids:
            raise ValueError(f"事件 {eid} 引用了不存在的关联企业")
        if etype == "joint_bid":
            refs = set(payload.get("leaders", [])) | set(payload.get("members", []))
            unknown = refs - bidder_ids
            if unknown:
                raise ValueError(f"事件 {eid} 联合体成员不存在：{sorted(unknown)}")

        if event.get("confidential") and not event.get("allowed_roles"):
            raise ValueError(f"涉密事件 {eid} 必须声明 allowed_roles")

    return value
