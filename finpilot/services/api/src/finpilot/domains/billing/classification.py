"""账单分类与交易口径（V5 第 10 节 S2-01／S2-02、T06／T07／T50）。

设计原则：

- **确定性**：同一输入永远得到同一 ``(category, rule_id, confidence)``；不调用模型、
  不依赖随机数、不依赖时钟。规则顺序固定，先命中者胜。
- **不改写原流水**：分类纠正写入 ``CategoryCorrection``，原 ``BillingTransaction.category``
  保持不变；报告读取时叠加纠正并标注 ``corrected: true``。
- **口径集中**：消费、现金流、内部互转、资产调拨、手续费、退款、未完成交易的判定
  都在本模块定义，服务层与异常层复用同一套判定，避免多处口径漂移。
- **不猜测**：退款不会用"同额同商户"做启发式关联；只有显式 ``linked_bt_id``
  才算已关联，其余标记 ``pending_verification``。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

CLASSIFIER_VERSION = "bill-classify-2026.10"

# --- 分类白名单（顺序即报表展示顺序） -----------------------------------

CATEGORIES: tuple[str, ...] = (
    "dining",
    "transport",
    "shopping",
    "housing",
    "entertainment",
    "medical",
    "education",
    "finance",
    "other",
)

CATEGORY_LABELS: dict[str, str] = {
    "dining": "餐饮",
    "transport": "交通",
    "shopping": "购物",
    "housing": "居住",
    "entertainment": "娱乐",
    "medical": "医疗",
    "education": "教育",
    "finance": "金融",
    "other": "其他",
}

DEFAULT_CATEGORY = "other"
CATEGORY_INDEX: dict[str, int] = {name: idx for idx, name in enumerate(CATEGORIES)}


def label_for(category: str) -> str:
    return CATEGORY_LABELS.get(category, CATEGORY_LABELS[DEFAULT_CATEGORY])


def is_known_category(value: Any) -> bool:
    return isinstance(value, str) and value in CATEGORIES


# --- 交易种类（kind）规范化 ---------------------------------------------

KIND_PURCHASE = "purchase"
KIND_INCOME = "income"
KIND_SALARY = "salary"
KIND_TRANSFER_INTERNAL = "transfer_internal"
KIND_TRANSFER_OUT = "transfer_out"
KIND_TRANSFER_IN = "transfer_in"
KIND_WEALTH_PURCHASE = "wealth_purchase"
KIND_WEALTH_REDEEM = "wealth_redeem"
KIND_FEE = "fee"
KIND_REFUND = "refund"
KIND_AA_SHARE = "aa_share"
KIND_SUBSCRIPTION = "subscription"
KIND_INTEREST = "interest"
KIND_PENDING = "pending"

#: 原始 kind → 规范 kind。未登记的取值原样保留（按方向归类为普通支出／收入）。
KIND_ALIASES: dict[str, str] = {
    "": KIND_PURCHASE,
    "purchase": KIND_PURCHASE,
    "consumption": KIND_PURCHASE,
    "card_purchase": KIND_PURCHASE,
    "pos": KIND_PURCHASE,
    "payment": KIND_PURCHASE,
    "expense": KIND_PURCHASE,
    "income": KIND_INCOME,
    "incoming": KIND_INCOME,
    "salary": KIND_SALARY,
    "payroll": KIND_SALARY,
    "interest": KIND_INTEREST,
    "transfer_internal": KIND_TRANSFER_INTERNAL,
    "internal_transfer": KIND_TRANSFER_INTERNAL,
    "own_transfer": KIND_TRANSFER_INTERNAL,
    "self_transfer": KIND_TRANSFER_INTERNAL,
    "transfer_out": KIND_TRANSFER_OUT,
    "transfer_in": KIND_TRANSFER_IN,
    "wealth_purchase": KIND_WEALTH_PURCHASE,
    "wealth_subscribe": KIND_WEALTH_PURCHASE,
    "fund_purchase": KIND_WEALTH_PURCHASE,
    "wealth_redeem": KIND_WEALTH_REDEEM,
    "wealth_redemption": KIND_WEALTH_REDEEM,
    "fund_redeem": KIND_WEALTH_REDEEM,
    "fee": KIND_FEE,
    "charge": KIND_FEE,
    "service_fee": KIND_FEE,
    "refund": KIND_REFUND,
    "refund_in": KIND_REFUND,
    "reversal": KIND_REFUND,
    "aa_share": KIND_AA_SHARE,
    "aa": KIND_AA_SHARE,
    "subscription": KIND_SUBSCRIPTION,
    "recurring": KIND_SUBSCRIPTION,
    "pending": KIND_PENDING,
    "hold": KIND_PENDING,
    "authorization_hold": KIND_PENDING,
    "unsettled": KIND_PENDING,
}

#: 未完成交易：金额已出现但业务状态尚未终结，报告必须单列。
INCOMPLETE_KINDS: frozenset[str] = frozenset({KIND_PENDING})

#: 自己账户之间的互转：账户现金流可见，用户合并消费中排除。
INTERNAL_TRANSFER_KINDS: frozenset[str] = frozenset({KIND_TRANSFER_INTERNAL})

#: 资产调拨：理财申购／赎回不算消费，也不算收入。
ASSET_ALLOCATION_KINDS: frozenset[str] = frozenset({KIND_WEALTH_PURCHASE, KIND_WEALTH_REDEEM})

#: 手续费：单列，不计入消费分类合计。
FEE_KINDS: frozenset[str] = frozenset({KIND_FEE})

#: 退款：发生在本期时按"现金流记退款入账 + 消费另列调整"处理。
REFUND_KINDS: frozenset[str] = frozenset({KIND_REFUND})

#: 收入类：现金流里天然属于入账，但不当成"消费"。
INCOME_KINDS: frozenset[str] = frozenset({KIND_INCOME, KIND_SALARY, KIND_INTEREST})


def _get(tx: Any, name: str, default: Any = None) -> Any:
    """同时支持 ORM 对象与 dict 的字段读取。"""
    if isinstance(tx, Mapping):
        return tx.get(name, default)
    return getattr(tx, name, default)


def normalize_kind(value: Any) -> str:
    raw = str(value or "").strip().lower()
    return KIND_ALIASES.get(raw, raw or KIND_PURCHASE)


def raw_kind(value: Any) -> str:
    return str(value or "").strip().lower()


def is_internal_transfer(kind: Any) -> bool:
    return normalize_kind(kind) in INTERNAL_TRANSFER_KINDS


def is_asset_allocation(kind: Any) -> bool:
    canonical = normalize_kind(kind)
    if canonical in ASSET_ALLOCATION_KINDS:
        return True
    # 容忍 "wealth_*" 家族扩展，避免新增产品种类时口径漏判
    return canonical.startswith("wealth_")


def is_fee(kind: Any) -> bool:
    return normalize_kind(kind) in FEE_KINDS


def is_refund(kind: Any) -> bool:
    return normalize_kind(kind) in REFUND_KINDS


def is_income(kind: Any) -> bool:
    return normalize_kind(kind) in INCOME_KINDS


def is_incomplete(kind: Any) -> bool:
    return normalize_kind(kind) in INCOMPLETE_KINDS


def direction_of(row: Any) -> str:
    """方向：优先 direction 字段；缺失时按 amount_minor 的符号推断。"""
    raw = str(_get(row, "direction", "") or "").strip().lower()
    if raw in ("in", "credit", "c"):
        return "in"
    if raw in ("out", "debit", "d"):
        return "out"
    amount = _get(row, "amount_minor", 0) or 0
    return "in" if int(amount) > 0 else "out"


def magnitude_minor(row: Any) -> int:
    """金额绝对值（分）。库内金额以正数 + direction 表达，负号也容忍。"""
    return abs(int(_get(row, "amount_minor", 0) or 0))


def signed_minor(row: Any) -> int:
    value = magnitude_minor(row)
    return value if direction_of(row) == "in" else -value


def is_refund_row(row: Any) -> bool:
    """退款判定：kind 为退款，或"入账且显式关联了原交易"。"""
    if is_refund(_get(row, "kind", "")):
        return True
    return direction_of(row) == "in" and bool(_get(row, "linked_bt_id", None))


def is_consumption_candidate(row: Any) -> bool:
    """是否为"用户消费"口径的候选：支出，且不是互转／资产调拨／手续费／退款。"""
    if direction_of(row) != "out":
        return False
    kind = _get(row, "kind", "")
    if is_internal_transfer(kind) or is_asset_allocation(kind) or is_fee(kind):
        return False
    return not is_refund_row(row)


def is_cashflow_inflow(row: Any) -> bool:
    return direction_of(row) == "in"


def is_cashflow_outflow(row: Any) -> bool:
    return direction_of(row) == "out"


# --- 关键字规则表（顺序固定，先命中者胜） -------------------------------

#: (rule_id, category, keywords)
KEYWORD_RULES: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    (
        "rule.keyword.dining",
        "dining",
        (
            "餐饮", "餐厅", "饭店", "外卖", "咖啡", "奶茶", "火锅", "烧烤", "快餐", "食堂",
            "食品", "烘焙", "面包", "星巴克", "麦当劳", "肯德基", "瑞幸", "美团外卖", "饿了么",
            "restaurant", "cafe", "coffee", "dining", "food", "starbucks",
        ),
    ),
    (
        "rule.keyword.transport",
        "transport",
        (
            "地铁", "公交", "出租车", "打车", "网约车", "滴滴", "高铁", "动车", "火车", "机票",
            "航空", "航班", "加油", "停车", "单车", "过路", "高速", "交通",
            "taxi", "metro", "railway", "airline", "fuel", "parking", "transport",
        ),
    ),
    (
        "rule.keyword.shopping",
        "shopping",
        (
            "超市", "商场", "百货", "购物", "淘宝", "天猫", "京东", "拼多多", "唯品会", "服饰",
            "服装", "鞋", "家电", "数码", "便利店", "母婴", "化妆品",
            "mall", "market", "supermarket", "shop", "retail",
        ),
    ),
    (
        "rule.keyword.housing",
        "housing",
        (
            "房租", "租金", "物业", "水费", "电费", "燃气", "暖气", "宽带", "家政", "维修",
            "居住", "房贷",
            "rent", "utility", "property", "water", "electricity", "gas", "broadband",
        ),
    ),
    (
        "rule.keyword.entertainment",
        "entertainment",
        (
            "电影", "影院", "游戏", "ktv", "视频", "音乐", "健身", "旅游", "酒店", "门票",
            "演出", "剧本", "密室", "娱乐", "会员",
            "entertainment", "cinema", "game", "music", "travel", "hotel", "ticket",
        ),
    ),
    (
        "rule.keyword.medical",
        "medical",
        (
            "医院", "医药", "药店", "诊所", "体检", "挂号", "医保", "口腔", "眼科", "药房",
            "医疗",
            "pharmacy", "hospital", "clinic", "medical",
        ),
    ),
    (
        "rule.keyword.education",
        "education",
        (
            "学费", "培训", "课程", "教育", "考试", "网校", "图书", "文具", "辅导", "驾校",
            "书本",
            "tuition", "course", "education", "training", "book",
        ),
    ),
    (
        "rule.keyword.finance",
        "finance",
        (
            "手续费", "利息", "保险", "保费", "理财", "基金", "证券", "股票", "还款", "贷款",
            "分期", "年费", "账户管理费", "外汇",
            "finance", "fee", "interest", "insurance", "fund", "loan", "repayment",
        ),
    ),
)

#: 字段 → 置信度。商户名比备注更可靠，对方户名再次之。
FIELD_CONFIDENCE: tuple[tuple[str, float], ...] = (
    ("merchant_name", 0.9),
    ("memo", 0.75),
    ("counterparty", 0.7),
)

STORED_CATEGORY_RULE = "rule.stored.valid_category"
CORRECTION_RULE = "rule.correction.user"
FALLBACK_RULE = "rule.fallback.other"


def classify(tx: Any) -> tuple[str, str, float]:
    """确定性分类：返回 ``(category, rule_id, confidence)``。

    优先级：已登记的原始分类 → 交易种类（互转／资产调拨／手续费／退款／收入）
    → 商户名／备注／对方户名的关键字规则 → 兜底"其他"。
    """
    kind = normalize_kind(_get(tx, "kind", ""))
    stored = str(_get(tx, "category", "") or "").strip().lower()
    if is_known_category(stored) and stored != DEFAULT_CATEGORY:
        return stored, STORED_CATEGORY_RULE, 1.0

    if is_internal_transfer(kind):
        return DEFAULT_CATEGORY, "rule.kind.internal_transfer", 1.0
    if is_asset_allocation(kind):
        return "finance", "rule.kind.asset_allocation", 1.0
    if is_fee(kind):
        return "finance", "rule.kind.fee", 1.0
    if is_refund(kind):
        # 退款分类跟随原交易；单独出现时无法归属，交给服务层按关联结果处理。
        return DEFAULT_CATEGORY, "rule.kind.refund", 0.5
    if is_income(kind):
        return DEFAULT_CATEGORY, "rule.kind.income", 1.0

    for field, confidence in FIELD_CONFIDENCE:
        text = str(_get(tx, field, "") or "").strip().lower()
        if not text:
            continue
        for rule_id, category, keywords in KEYWORD_RULES:
            for keyword in keywords:
                if keyword in text:
                    return category, rule_id, confidence

    return DEFAULT_CATEGORY, FALLBACK_RULE, 0.3


def explain_classification(tx: Any) -> dict[str, Any]:
    """分类的可解释结果，供报告展示"为什么归到这一类"。"""
    category, rule_id, confidence = classify(tx)
    return {
        "category": category,
        "label": label_for(category),
        "rule_id": rule_id,
        "confidence": confidence,
        "classifier_version": CLASSIFIER_VERSION,
        "deterministic": True,
    }


__all__ = [
    "ASSET_ALLOCATION_KINDS",
    "CATEGORIES",
    "CATEGORY_INDEX",
    "CATEGORY_LABELS",
    "CLASSIFIER_VERSION",
    "CORRECTION_RULE",
    "DEFAULT_CATEGORY",
    "FEE_KINDS",
    "INCOME_KINDS",
    "INCOMPLETE_KINDS",
    "INTERNAL_TRANSFER_KINDS",
    "KEYWORD_RULES",
    "REFUND_KINDS",
    "classify",
    "direction_of",
    "explain_classification",
    "is_asset_allocation",
    "is_cashflow_inflow",
    "is_cashflow_outflow",
    "is_consumption_candidate",
    "is_fee",
    "is_incomplete",
    "is_income",
    "is_internal_transfer",
    "is_known_category",
    "is_refund",
    "is_refund_row",
    "label_for",
    "magnitude_minor",
    "normalize_kind",
    "raw_kind",
    "signed_minor",
]