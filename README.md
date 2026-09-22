# 低价投标质量预警

识别制造业低价竞争背后的交付与质量风险。系统把招标规则、报价版本、可验证成本项、产能、历史交付、检验结果、投诉与关联企业放在同一次研判中，输出**候选解释与尚缺证据**，不对企业作违规定性。

## 资料约定

| 文件 | 作用 |
| --- | --- |
| `contracts/domain.schema.json` | 领域背景资料字段结构 |
| `contracts/case.schema.json` | 案件事件流字段结构（15 类事件） |
| `fixtures/domain.json` | 领域背景样例 |
| `fixtures/case.json` | 不含真实个人信息的案件样例（虚构企业与编号） |
| `src/domain.py` | 领域资料载入校验 |
| `src/case.py` | 案件事件流载入与引用完整性校验 |
| `src/assessment.py` | 风险研判引擎 |

## 研判语义

**事实只追加，快照不可变。** 所有事实以事件进入日志，每个事件带 `occurred_at`（发生时间）和 `received_at`（接收时间）。价格执法、质量监管的线索迟到归集、申诉补证、原料价格突变都按接收时间更新判断；每次研判生成不可变快照并保留 `based_on` 依据清单与 `prior_snapshot` 链，历史结论的依据不被覆盖。

**输出候选解释，不是单一分数。** 对最低报价人同时给出三种解释：

- `efficiency_advantage` 效率优势：工艺或管理效率带来真实成本下降
- `short_term_promotion` 短期促销：锁价、让利等阶段性安排
- `unsustainable_undercutting` 不可持续压价：低于成本竞标，履约中存在降标风险

每种解释分别列出**支持证据、反证、尚缺证据**和一句话状态（正反证据并存 / 有支持性证据，反证不足 / …）。报价与主材成本的差额只作为 `price_fact` 事实测算呈现，不构成结论。

**关联企业归并但不株连。** 关联企业的执法、质量记录会进入研判并标注"关联记录，不视为本主体行为"，另列"与本主体实际隔离情况"为待补证据。

**商业秘密按授权可见。** `confidential` 事件必须声明 `allowed_roles`；未授权角色看不到内容，快照中仅出现 `redacted_for_role` 屏蔽提示。

**豁免不消除疑点。** 人工豁免只把案件置于观察期；豁免到期自动回到复核队列，观察期内出现不合格交付则提前回队。

**后续交付持续校正。** 到货检验合格/不合格作为新事件增强或削弱既有解释（如"整改后交付合格，信号减弱"），但原因调查与责任认定始终保留为待补证据。

## 使用方式

```python
from pathlib import Path
from src.case import load_case
from src.assessment import AssessmentEngine

case = load_case(Path("fixtures/case.json"))
eng = AssessmentEngine(case, viewer_role="经办人")
bidder = eng.lowest_bidder("2026-03-11")
snapshot = eng.assess(bidder, "2026-03-11")
```

`viewer_role="审查人员"` 时可看到授权范围内的锁价协议等商业秘密；其他角色只能看到屏蔽提示。重复调用 `assess` 会生成 S1、S2… 快照链，全部留存在 `eng.history`。

## 本地校验

```bash
python -m unittest discover -s tests
```

仓库不保存账号、密钥、连接串或真实身份资料。
