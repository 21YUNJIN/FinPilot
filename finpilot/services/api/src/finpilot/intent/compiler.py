"""GoalSpec 编译与澄清（V5 5.1、5.2、PLAN-01、PLAN-02、PLAN-07、S1-01）。

设计：默认使用确定性规则编译器（可在无模型环境下复现）；
live 模式下可由模型产出候选，但候选同样要经本模块的结构校验，模型不能创造权限。

输出严格区分：目标 goals、硬约束 hard_constraints、偏好 preferences、事实 facts、
待澄清 unresolved 与推断 inferred。关键字段（金额、对象、账户、日期）未确定时
禁止执行写操作，只能提问。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..errors import FinPilotError
from ..ids import new_id
from ..models import Card, Mandate, Payee, Subscription, WealthProduct
from ..money import format_minor, parse_amount_minor
from ..timeutil import SHANGHAI, business_day, format_iso, utcnow
from ..kernel.identity import AuthContext
from ..kernel.snapshots import get_snapshot

SCHEMA_VERSION = "1.0"

# 场景识别关键词
SCENE_KEYWORDS: dict[str, tuple[str, ...]] = {
    "S1_transfer": ("转账", "转给", "转", "汇款", "打钱", "付款", "给他", "给她", "还钱", "借"),
    "S1_schedule": ("定时", "预约转", "自动转", "到点", "明天转", "后天转", "下个月转", "每月转"),
    "S1_aa": ("aa", "AA", "分摊", "均分", "一起付", "各付各", "拼单", "凑钱"),
    "S2_bill": ("账单", "花了多少", "消费", "开销", "支出", "月报", "年报", "流水", "支出分析", "钱花哪"),
    "S3_wealth": ("理财", "基金", "申购", "赎回", "收益", "风险测评", "测评", "理财产品", "投资", "净值", "波动"),
    "S4_card": ("卡", "信用卡", "挂失", "补卡", "额度", "虚拟卡", "申请卡", "限额", "解挂", "卡片", "交易限制"),
    "S5_subscription": ("订阅", "会员", "自动续费", "代扣", "续费", "取消订阅", "扣费"),
    "S6_cross": ("生日", "鲜花", "蛋糕", "聚会", "预订", "订花", "订蛋糕", "餐厅", "party", "纪念日"),
}

UNSUPPORTED_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"炒股|买股票|开户炒股|证券|基金定投实盘|期货|外汇|比特币|加密货币|虚拟币", "securities_or_crypto"),
    (r"贷款|借钱给我|授信|提额到|套现|分期套现", "credit_or_loan"),
    (r"保证收益|稳赚|包赚|保本高息", "guaranteed_return"),
    (r"帮我查别人|其他人的账户|他的余额|查一下我老婆|查一下我老公的卡", "other_user_data"),
    # 间接注入常见形态：把账单/身份/验证码等送往外部地址或第三方（必须带外部目的地，避免误伤"转发给我"）
    (r"(发送|上传|转发|导出|提交|回传).{0,24}(https?://|attacker\.|@[a-z0-9.-]+\.[a-z]{2,})", "exfiltration_request"),
    (r"(账单|身份|身份证|验证码|密码|密钥|令牌).{0,16}(发送|上传|转发|发|传).{0,12}(https?://|attacker|外部|第三方)", "exfiltration_request"),
    (r"改一下(政策|规则|权限)|把(额度|限额)调高到|sudo|管理员权限", "policy_change"),
    (r"跳过(审批|确认|授权|核验)|不用(确认|审批|授权)|不需要(确认|审批)|别让我确认|无需确认|免确认", "approval_bypass"),
)

VAGUE_PATTERNS = ("你看着办", "都可以", "随便", "你决定", "都行", "看着来", "怎么都行")

# Model output is only a suggestion. A write-like suggestion must also have an
# independently identifiable action in the original message.
LIVE_WRITE_EVIDENCE = {
    "transfer": r"转账|转给|汇款|打钱|还钱|付款",
    "schedule_transfer": r"转账|转给|汇款|打钱|预约转|自动转|明天转|后天转",
    "create_aa_request": r"分摊|均分|一起付|各付各|拼单|凑钱|(?i:aa)",
    "purchase_wealth": r"申购|购买|买入|买理财|买基金",
    "redeem_wealth": r"赎回",
    "card_apply": r"申请|办卡|开卡",
    "card_lost_report": r"挂失|解挂",
    "card_replace": r"补卡",
    "card_limit_change": r"调整额度|调低额度|调高额度|提高额度|降低额度|提升额度|减少额度",
    "card_control": r"限制|锁卡|解锁",
    "cancel_subscription": r"取消|退订|别再扣",
    "plan_event": r"预订|订购|买|订|预定",
}

_CN_DIGITS = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
_CN_UNITS = {"十": 10, "百": 100, "千": 1000, "万": 10000}

WEEKDAY_MAP = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "日": 7, "天": 7}


@dataclass
class IntentContext:
    session: Session
    auth: AuthContext
    mode: str = "mock"
    history: list[dict] = field(default_factory=list)


def normalize(text: str) -> str:
    table = {"　": " ", "：": ":", "，": ",", "。": ".", "！": "!", "？": "?", "（": "(", "）": ")"}
    out = text.strip()
    for src, dst in table.items():
        out = out.replace(src, dst)
    # 全角数字与字母
    out = "".join(chr(ord(ch) - 0xFEE0) if "０" <= ch <= "９" or "Ａ" <= ch <= "Ｚ" or "ａ" <= ch <= "ｚ" else ch for ch in out)
    return out


def parse_chinese_number(text: str) -> int | None:
    """解析中文数字（支持 十/百/千/万 与 零一二三…）。"""
    if not text:
        return None
    if not any(ch in _CN_DIGITS or ch in _CN_UNITS for ch in text):
        return None
    total = 0
    section = 0
    number = 0
    for ch in text:
        if ch in _CN_DIGITS:
            number = _CN_DIGITS[ch]
        elif ch in _CN_UNITS:
            unit = _CN_UNITS[ch]
            if unit == 10000:
                section = (section + (number or 1 if number == 0 and section == 0 else number)) * unit
                total += section
                section = 0
                number = 0
            else:
                section += (number or 1) * unit
                number = 0
        else:
            return None
    return total + section + number


def extract_amount(text: str) -> tuple[int | None, str | None]:
    """返回 (amount_minor, 原文片段)。支持 '100元'、'100.5块'、'一百元'、'1,000 元'。"""
    cleaned = text.replace(",", "")
    match = re.search(r"(\d+(?:\.\d+)?)\s*(?:元|块钱|块|元钱|rmb|RMB)", cleaned)
    if match:
        return parse_amount_minor(match.group(1)), match.group(0)
    match = re.search(r"([零一二两三四五六七八九十百千万]+)\s*(?:元|块钱|块)", cleaned)
    if match:
        value = parse_chinese_number(match.group(1))
        if value is not None:
            return value * 100, match.group(0)
    return None, None


def extract_all_amounts(text: str) -> list[tuple[int, str]]:
    cleaned = text.replace(",", "")
    found: list[tuple[int, str]] = []
    for match in re.finditer(r"(\d+(?:\.\d+)?)\s*(?:元|块钱|块|元钱)", cleaned):
        found.append((parse_amount_minor(match.group(1)), match.group(0)))
    return found


def extract_budget(text: str) -> tuple[int | None, str | None]:
    match = re.search(r"(?:预算|最多|不超过|上限|控制在|花费|大概)\s*(?:是|为)?\s*(\d+(?:\.\d+)?)\s*(?:元|块钱|块)", text)
    if match:
        return parse_amount_minor(match.group(1)), match.group(0)
    match = re.search(r"(?:预算|最多|不超过|上限|控制在)\s*(?:是|为)?\s*([零一二两三四五六七八九十百千万]+)\s*(?:元|块钱|块)", text)
    if match:
        value = parse_chinese_number(match.group(1))
        if value is not None:
            return value * 100, match.group(0)
    return None, None


def extract_floor(text: str) -> tuple[int | None, str | None]:
    match = re.search(r"(?:应急金|留(?:着|下)|保留|底线|不动)\s*(?:是|为)?\s*(\d+(?:\.\d+)?)\s*(?:元|块钱|块)", text)
    if match:
        return parse_amount_minor(match.group(1)), match.group(0)
    return None, None


def extract_datetime(text: str, reference: datetime) -> tuple[datetime | None, str | None, bool]:
    """返回 (绝对时间, 原文, 是否有歧义)。相对日期一律基于任务参考时间解析。"""
    local = reference.astimezone(SHANGHAI)
    base_date = local.date()
    ambiguous = False

    day_offset = None
    snippet = None
    if "今天" in text or "今日" in text:
        day_offset, snippet = 0, "今天"
    elif "后天" in text:
        day_offset, snippet = 2, "后天"
    elif "明天" in text or "明日" in text:
        day_offset, snippet = 1, "明天"
    elif "大后天" in text:
        day_offset, snippet = 3, "大后天"

    target_date = None
    if day_offset is not None:
        target_date = base_date + timedelta(days=day_offset)
    else:
        match = re.search(r"(下下|下|这|本)?(?:周|星期|礼拜)([一二三四五六日天])", text)
        if match:
            prefix, weekday_char = match.group(1) or "", match.group(2)
            weekday = WEEKDAY_MAP[weekday_char]
            delta = (weekday - base_date.isoweekday()) % 7
            if prefix == "下":
                delta = delta + 7 if delta != 0 or True else delta
                delta = ((weekday - base_date.isoweekday()) % 7) + 7
            elif prefix == "下下":
                delta = ((weekday - base_date.isoweekday()) % 7) + 14
            elif prefix in ("这", "本"):
                delta = (weekday - base_date.isoweekday()) % 7
            target_date = base_date + timedelta(days=delta)
            snippet = match.group(0)
        else:
            match = re.search(r"(\d{1,4})[-/年](\d{1,2})[-/月](\d{1,2})", text)
            if match:
                year = int(match.group(1))
                if year < 100:
                    year += 2000
                target_date = datetime(year, int(match.group(2)), int(match.group(3))).date()
                snippet = match.group(0)
            else:
                match = re.search(r"(\d{1,2})月(\d{1,2})[日号]", text)
                if match:
                    target_date = datetime(base_date.year, int(match.group(1)), int(match.group(2))).date()
                    snippet = match.group(0)
                else:
                    match = re.search(r"(\d{1,2})[日号](?:的)?", text)
                    if match and "月" not in text:
                        day = int(match.group(1))
                        if 1 <= day <= 31:
                            month = base_date.month
                            year = base_date.year
                            if day < base_date.day:
                                month += 1
                                if month > 12:
                                    month = 1
                                    year += 1
                            try:
                                target_date = datetime(year, month, day).date()
                                snippet = match.group(0)
                            except ValueError:
                                ambiguous = True

    hour, minute = 9, 0
    time_found = False
    match = re.search(r"(上午|早上|中午|下午|傍晚|晚上)?\s*(\d{1,2})\s*[点:时]\s*(半|\d{1,2})?", text)
    if match:
        period, hour_text, minute_text = match.group(1), match.group(2), match.group(3)
        hour = int(hour_text)
        if period in ("下午", "傍晚", "晚上") and hour < 12:
            hour += 12
        if period in ("上午", "早上") and hour == 12:
            hour = 0
        if minute_text == "半":
            minute = 30
        elif minute_text:
            minute = int(minute_text)
        time_found = True

    if target_date is None:
        if time_found and day_offset is None and "明天" not in text:
            # 只有时间没有日期：默认今天；若已过则明天，并标记需要确认
            candidate = datetime.combine(base_date, datetime.min.time(), tzinfo=SHANGHAI).replace(hour=hour, minute=minute)
            if candidate <= local:
                candidate += timedelta(days=1)
                ambiguous = True
            return candidate.astimezone(reference.tzinfo), snippet or f"{hour:02d}:{minute:02d}", ambiguous
        return None, snippet, ambiguous

    moment = datetime.combine(target_date, datetime.min.time(), tzinfo=SHANGHAI).replace(hour=hour, minute=minute)
    if not time_found:
        ambiguous = True  # 只有日期没有时刻，需要用户确认或使用默认时刻
    return moment.astimezone(reference.tzinfo), snippet, ambiguous


def detect_scene(text: str) -> tuple[str, list[str]]:
    scored: list[tuple[int, str]] = []
    for scene, keywords in SCENE_KEYWORDS.items():
        hits = [kw for kw in keywords if kw in text]
        if hits:
            scored.append((len(hits), scene))
    if not scored:
        return "unknown", []
    scored.sort(key=lambda item: (-item[0], item[1]))
    top = scored[0]
    matched = [kw for kw in SCENE_KEYWORDS[top[1]] if kw in text]
    return top[1], matched


def detect_unsupported(text: str) -> tuple[str | None, str | None]:
    for pattern, reason in UNSUPPORTED_PATTERNS:
        match = re.search(pattern, text)
        if match:
            return reason, match.group(0)
    return None, None


def _is_vague(text: str) -> bool:
    return any(token in text for token in VAGUE_PATTERNS)


def resolve_payees(session: Session, user_id: str, text: str, history: list[dict]) -> dict:
    """解析收款人线索：姓名、别名、手机号与上下文指代。歧义必须先澄清。"""
    payees = session.execute(select(Payee).where(Payee.owner_user_id == user_id)).scalars().all()
    matches: list[Payee] = []

    for payee in payees:
        if payee.display_name and payee.display_name in text:
            matches.append(payee)
            continue
        if payee.phone_ref and payee.phone_ref in text:
            matches.append(payee)
            continue
        if any(alias and alias in text for alias in (payee.aliases or [])):
            matches.append(payee)

    if not matches:
        # 手机号后四位
        phone_tail = re.search(r"(\d{4})(?!\d)", text)
        if phone_tail:
            candidates = [p for p in payees if p.phone_ref.endswith(phone_tail.group(1))]
            if len(candidates) == 1:
                matches = candidates

    pronoun = bool(re.search(r"(给他|给她|给他们|转给他|还他|还她|同上|还是他|还是她)", text))
    if not matches and pronoun and history:
        for item in reversed(history):
            payee_id = (item.get("structured") or {}).get("payee_id") or item.get("payee_id")
            if payee_id:
                found = next((p for p in payees if p.payee_id == payee_id), None)
                if found is not None:
                    matches = [found]
                    break

    def view(payee: Payee) -> dict:
        return {
            "payee_id": payee.payee_id,
            "display_name": payee.display_name,
            "masked_account": payee.destination_account_ref,
            "phone_tail": payee.phone_ref[-4:] if payee.phone_ref else "",
            "bank_name": payee.bank_name,
        }

    if len(matches) == 1:
        return {"status": "resolved", "payee": view(matches[0]), "candidates": []}
    if len(matches) > 1:
        return {"status": "ambiguous", "payee": None, "candidates": [view(p) for p in matches]}
    name_candidate = re.search(r"(?:给|转给|转|付款给)\s*([\u4e00-\u9fa5]{2,4})", text)
    if name_candidate:
        return {"status": "not_found", "payee": None, "candidates": [], "name": name_candidate.group(1)}
    return {"status": "missing", "payee": None, "candidates": []}


def resolve_product(session: Session, text: str) -> dict:
    """把用户口中的产品名解析成 product_id。

    只做**完整产品名**匹配：不按"理财""基金"这类泛词猜产品，也不做相似度猜测；
    命中多个时要求澄清，命中零个时如实返回 not_found（由规划器判不可行）。
    """
    products = session.execute(select(WealthProduct).where(WealthProduct.is_active.is_(True))).scalars().all()
    matches = [product for product in products if product.name and product.name in text]
    if len(matches) == 1:
        product = matches[0]
        return {
            "status": "resolved",
            "product": {
                "product_id": product.product_id,
                "name": product.name,
                "issuer_risk_level": product.issuer_risk_level,
                "min_minor": product.min_minor,
            },
        }
    if len(matches) > 1:
        return {
            "status": "ambiguous",
            "candidates": [
                {"value": product.product_id, "label": f"{product.name}（{product.issuer_risk_level}）"}
                for product in matches
            ],
        }
    return {"status": "not_found"}

def resolve_card(session: Session, user_id: str, text: str, *, require_explicit: bool = False) -> dict:
    """把"那张卡"解析成具体卡片：只有尾号/卡种等显式线索才定位，否则要求澄清。

    ``require_explicit`` 用于锁卡/挂失/调额这类高影响操作：带"那张/这张/我的"这类指代时，
    即使名下只有一张有效卡也要求用户明确，避免默认落到某一张卡上。
    """
    cards = session.execute(select(Card).where(Card.owner_user_id == user_id)).scalars().all()
    if not cards:
        return {"status": "none", "card": None, "candidates": []}

    compact = text.replace(" ", "")
    by_tail = [card for card in cards if card.masked_pan and card.masked_pan.replace(" ", "")[-4:] in compact]
    if len(by_tail) == 1:
        return {"status": "resolved", "card": by_tail[0], "candidates": []}
    if len(by_tail) > 1:
        return {"status": "ambiguous", "card": None, "candidates": [_card_option(card) for card in by_tail]}

    if "虚拟" in text:
        virtual = [card for card in cards if (card.brand or "").upper() == "VIRTUAL"]
        if len(virtual) == 1:
            return {"status": "resolved", "card": virtual[0], "candidates": []}
        if virtual:
            return {"status": "ambiguous", "card": None, "candidates": [_card_option(card) for card in virtual]}

    if require_explicit and any(token in text for token in ("那", "这", "我的")):
        pool = [card for card in cards if card.lifecycle_status == "active"] or cards
        return {"status": "ambiguous", "card": None, "candidates": [_card_option(card) for card in pool]}

    active = [card for card in cards if card.lifecycle_status == "active"]
    if len(active) == 1:
        return {"status": "resolved", "card": active[0], "candidates": []}
    pool = active or cards
    return {"status": "ambiguous", "card": None, "candidates": [_card_option(card) for card in pool]}


def _card_option(card: Card) -> dict:
    tail = (card.masked_pan or "").replace(" ", "")[-4:]
    return {"value": card.card_id, "label": f"尾号 {tail}（{card.lifecycle_status}，{card.brand}）"}


def resolve_subscription(session: Session, user_id: str, text: str) -> dict:
    """把"那个订阅"解析成具体订阅：按商户名匹配；只剩一个有效订阅时可直接定位。"""
    rows = session.execute(
        select(Subscription).where(Subscription.owner_user_id == user_id, Subscription.status == "active")
    ).scalars().all()
    if not rows:
        return {"status": "none", "subscription": None, "candidates": []}

    by_name = [row for row in rows if row.merchant_name and row.merchant_name in text]
    if len(by_name) == 1:
        return {"status": "resolved", "subscription": by_name[0], "candidates": []}
    if len(by_name) > 1:
        return {"status": "ambiguous", "subscription": None, "candidates": [_subscription_option(row) for row in by_name]}
    if len(rows) == 1:
        return {"status": "resolved", "subscription": rows[0], "candidates": []}
    return {"status": "ambiguous", "subscription": None, "candidates": [_subscription_option(row) for row in rows]}


def _subscription_option(row: Subscription) -> dict:
    return {"value": row.subscription_id, "label": f"{row.merchant_name}（{format_minor(row.amount_minor)} 元/{row.period}）"}


def _clarify_card(
    session: Session, user_id: str, text: str, facts: dict, unresolved: list[dict], *, require_explicit: bool = True
) -> None:
    resolved = resolve_card(session, user_id, text, require_explicit=require_explicit)
    if resolved["status"] == "resolved":
        facts["card_id"] = resolved["card"].card_id
    elif resolved["status"] == "ambiguous":
        unresolved.append(_clarification("card_id", "要操作哪一张卡？", resolved["candidates"]))


def _clarify_subscription(session: Session, user_id: str, text: str, facts: dict, unresolved: list[dict]) -> None:
    resolved = resolve_subscription(session, user_id, text)
    if resolved["status"] == "resolved":
        subscription = resolved["subscription"]
        facts["subscription_id"] = subscription.subscription_id
        mandate = session.execute(
            select(Mandate).where(
                Mandate.owner_user_id == user_id,
                Mandate.subscription_id == subscription.subscription_id,
                Mandate.status == "active",
            )
        ).scalars().first()
        if mandate is not None:
            facts["mandate_id"] = mandate.mandate_id
    elif resolved["status"] == "ambiguous":
        unresolved.append(_clarification("subscription_id", "要取消哪一个订阅？", resolved["candidates"]))

def _clarification(field_name: str, question: str, options: list[dict] | None = None, *, blocking: bool = True) -> dict:
    return {
        "clarification_id": new_id("clar"),
        "field": field_name,
        "question": question,
        "options": options or [],
        "blocking": blocking,
        "answered": False,
        "answer": None,
    }


def compile_goal(context: IntentContext, message: str, history: list[dict] | None = None, reference_time: datetime | None = None, *, model_hint=None) -> dict:
    """把自然语言编译为结构化 GoalSpec；不确定的关键字段进入 unresolved。"""
    session = context.session
    auth = context.auth
    history = history if history is not None else context.history
    reference = reference_time or utcnow()
    text = normalize(message)

    scene, matched_keywords = detect_scene(text)
    unsupported_reason, unsupported_span = detect_unsupported(text)
    # mock 模式（无模型提示）此前不检查否定表达，导致"先别…转账"仍会规划出一笔转账。
    # live 模式由下面的模型提示分支单独处理（冲突时进入澄清），这里不重复拦截。
    if model_hint is None:
        negation_hit = re.search(
            r"(?:不要|别|不想|不需要|禁止|先别)[^,，。.!！?？]{0,20}(?:转|汇款|打钱|申购|购买|买入|赎回|申请|挂失|解挂|取消|退订|锁)",
            text,
        )
        if negation_hit and not unsupported_reason:
            unsupported_reason, unsupported_span = "negated_request", negation_hit.group(0)

    # 他人账单：只在"某某的账单"里由**人名**触发；上个月/这个月这类时间词不算人名。
    if not unsupported_reason:
        name_bill = re.search(r"([\u4e00-\u9fa5]{2,4})的(账单|报表|报告|消费记录|流水)", text)
        if name_bill:
            token = name_bill.group(1)
            time_words = ("月", "年", "周", "天", "季度", "最近", "我", "你", "他", "她", "它")
            if not any(word in token for word in time_words):
                unsupported_reason, unsupported_span = "other_user_data", name_bill.group(0)
    hint_action = None
    model_clarification = False
    if model_hint is not None and not unsupported_reason:
        if model_hint.uncertain or model_hint.scene == "unknown":
            scene, matched_keywords, model_clarification = "unknown", [], True
        elif scene != "unknown" and scene != model_hint.scene:
            # Conflicting interpretations are not permission to guess an action.
            scene, matched_keywords, model_clarification = "unknown", [], True
        elif re.search(r"(?:不要|别|不想|不需要|禁止)[^,，。.!！?？]{0,20}(?:转|汇款|打钱|申购|购买|买入|赎回|申请|挂失|解挂|取消|退订)", text):
            scene, matched_keywords, model_clarification = "unknown", [], True
        elif model_hint.action in LIVE_WRITE_EVIDENCE and (re.search(r"如果|假如|要是", text) or not re.search(LIVE_WRITE_EVIDENCE[model_hint.action], text)):
            scene, matched_keywords, model_clarification = "unknown", [], True
        else:
            scene, hint_action = model_hint.scene, model_hint.action
    snapshot = get_snapshot(session, auth)

    goals: list[str] = []
    hard_constraints: dict = {
        "currency": "CNY",
        "budget_includes_fees": True,
        "protected_floor_minor": snapshot["protected_floor_minor"],
        "protected_floor_scope": snapshot["protection_scope"],
    }
    preferences: list[dict] = []
    unresolved: list[dict] = []
    evidence: list[dict] = []
    inferred: list[dict] = []
    facts: dict = {}

    amount_minor, amount_span = extract_amount(text)
    budget_minor, budget_span = extract_budget(text)
    floor_minor, floor_span = extract_floor(text)
    when, when_span, when_ambiguous = extract_datetime(text, reference)

    if floor_minor is not None:
        hard_constraints["protected_floor_minor"] = floor_minor
        evidence.append({"field": "hard_constraints.protected_floor_minor", "value": floor_minor, "span": floor_span, "source": "user_message"})

    funding_accounts = [a["account_id"] for a in snapshot["accounts"] if a["status"] == "active"]
    default_account = next(
        (a["account_id"] for a in snapshot["accounts"] if a["account_id"] == "account_salary"),
        funding_accounts[0] if funding_accounts else None,
    )
    # 用户显式指定账户时按名称／类型匹配；未指定则用默认账户
    selected_account = None
    for account in snapshot["accounts"]:
        name = account.get("name") or ""
        if name and name in text:
            selected_account = account["account_id"]
            break
    if selected_account is None:
        if "储蓄卡" in text or "savings" in text.lower():
            selected_account = next((a for a in funding_accounts if "savings" in a), None)
        elif "工资卡" in text:
            selected_account = next((a for a in funding_accounts if "salary" in a), None)
    if selected_account:
        default_account = selected_account
        evidence.append({"field": "facts.source_account_id", "value": selected_account, "source": "user_message"})

    if unsupported_reason:
        goals.append("unsupported_request")
        if unsupported_reason == "negated_request":
            question = "检测到否定/取消表达，我不会执行这笔操作；如果确实要办理，请去掉否定词后重新说明。"
        elif unsupported_reason == "approval_bypass":
            question = "不能跳过确认或授权：资金、卡片与代扣操作都必须由你本人在服务端确认后执行。"
        elif unsupported_reason == "exfiltration_request":
            question = "不会把账单、身份信息、验证码或密钥发送到外部地址；这类请求一律拒绝。"
        else:
            question = "这个需求不在本版本支持范围内（不含证券下单、授信、跨币种与保证收益）。我可以帮你在已支持的能力内换一种做法。"
        unresolved.append(_clarification("unsupported", question, blocking=False))
        facts["unsupported_reason"] = unsupported_reason
        facts["unsupported_span"] = unsupported_span

    if scene == "S1_transfer" or scene == "S1_schedule":
        goals.append("transfer")
        resolution = resolve_payees(session, auth.user_id, text, history)
        facts["payee_resolution"] = resolution["status"]
        if resolution["status"] == "ambiguous":
            unresolved.append(
                _clarification(
                    "payee_id",
                    "有多个同名联系人，请选择收款对象（已脱敏）。",
                    options=[
                        {"value": c["payee_id"], "label": f"{c['display_name']} · {c['masked_account']} · 尾号{c['phone_tail']}"}
                        for c in resolution["candidates"]
                    ],
                )
            )
            facts["payee_candidates"] = resolution["candidates"]
        elif resolution["status"] == "not_found":
            unresolved.append(
                _clarification(
                    "payee_id",
                    f"没有找到收款人「{resolution.get('name')}」。新增收款人需要你确认银行验证后的目标账户摘要。",
                    blocking=True,
                )
            )
        elif resolution["status"] == "missing":
            unresolved.append(_clarification("payee_id", "请告诉我要转给谁（姓名、手机号或备注名）。"))
        else:
            facts["payee_id"] = resolution["payee"]["payee_id"]
            facts["payee_view"] = resolution["payee"]
            evidence.append({"field": "facts.payee_id", "value": resolution["payee"]["payee_id"], "source": "contacts"})

        amount = amount_minor
        if amount is None and budget_minor is not None:
            amount = budget_minor
            inferred.append({"field": "transfer.amount_minor", "value": amount, "reason": "从预算推断金额，需要用户确认"})
        if amount is None:
            if _is_vague(text):
                unresolved.append(
                    _clarification(
                        "amount_minor",
                        "「你看着办」无法作为授权依据。请给出明确金额、收款人和时间，我才能准备确认摘要。",
                    )
                )
            else:
                unresolved.append(_clarification("amount_minor", "请给出转账金额（元，最多两位小数）。"))
        else:
            hard_constraints["amount_minor"] = amount
            evidence.append({"field": "hard_constraints.amount_minor", "value": amount, "span": amount_span, "source": "user_message"})

        facts["source_account_id"] = default_account
        if when is not None or scene == "S1_schedule" or any(k in text for k in ("明天", "后天", "定时", "预约", "下个月", "每月")):
            if when is None:
                unresolved.append(_clarification("execute_after", "请确认定时转账的执行日期与时刻。"))
            else:
                goals.append("schedule_transfer")
                hard_constraints["execute_after"] = format_iso(when)
                evidence.append({"field": "hard_constraints.execute_after", "value": format_iso(when), "span": when_span, "source": "parsed_relative_date"})
                if when_ambiguous:
                    unresolved.append(
                        _clarification(
                            "execute_after",
                            f"我理解为 {format_iso(when)}，请确认执行时刻（时间是为你的任务参考时间推算的）。",
                            options=[{"value": format_iso(when), "label": format_iso(when)}, {"value": "other", "label": "换一个时间"}],
                        )
                    )
        if "备注" in text or "memo" in text.lower():
            memo_match = re.search(r"备注[:：]?\s*([^\s,，。]{1,20})", text)
            if memo_match:
                facts["memo"] = memo_match.group(1)

    elif scene == "S1_aa":
        goals.append("create_aa_request")
        if amount_minor is None:
            unresolved.append(_clarification("final_cost_minor", "AA 只能按已确认的实际支出分摊，请给出实际支付金额。"))
        else:
            hard_constraints["final_cost_minor"] = amount_minor
        participants = re.findall(r"([\u4e00-\u9fa5]{2,4})(?:、|,|和|跟)", text)
        count_match = re.search(r"(\d+)\s*(?:个)?人", text)
        participant_count = int(count_match.group(1)) if count_match else (len(participants) + 1 if participants else None)
        if participant_count is None or participant_count < 2:
            unresolved.append(_clarification("participants", "请给出参与分摊的人数和名单（发起人是否包含在内）。"))
        else:
            hard_constraints["participant_count"] = participant_count
            facts["participants"] = participants

    elif scene == "S2_bill":
        goals.append("analyze_bill")
        period = {"kind": "month", "anchor": reference.astimezone(SHANGHAI).strftime("%Y-%m")}
        if "年" in text and "月" not in text:
            period = {"kind": "year", "anchor": reference.astimezone(SHANGHAI).strftime("%Y")}
        elif "去年" in text:
            period = {"kind": "year", "anchor": str(reference.astimezone(SHANGHAI).year - 1)}
        elif "上个月" in text:
            first = reference.astimezone(SHANGHAI).replace(day=1)
            prev = first - timedelta(days=1)
            period = {"kind": "month", "anchor": prev.strftime("%Y-%m")}
        elif "自定义" in text or "到" in text and "月" in text:
            dates = re.findall(r"(\d{1,2})月", text)
            if len(dates) >= 2:
                period = {
                    "kind": "custom",
                    "start": f"{reference.year}-{int(dates[0]):02d}-01",
                    "end": f"{reference.year}-{int(dates[1]):02d}-01",
                }
        facts["period"] = period
        facts["basis"] = "both" if ("现金流" in text or "两种" in text) else "consumption"
        # "看看最近"没有给出区间：默认当月会掩盖歧义，先问清账期
        if "最近" in text and not re.search(r"(个月|月|年|季度|周|天)", text):
            unresolved.append(
                _clarification(
                    "period",
                    "“最近”指哪个区间？请给出账期，例如本月、上月、最近 3 个月，或具体起止月份。",
                )
            )

    elif scene == "S3_wealth":
        if hint_action == "risk_assess" or (not hint_action and ("测评" in text or "问卷" in text)):
            goals.append("risk_assess")
        elif hint_action == "redeem_wealth" or (not hint_action and "赎回" in text):
            goals.append("redeem_wealth")
            if amount_minor is None:
                unresolved.append(_clarification("redeem_amount_minor", "请给出赎回金额或份额。"))
            else:
                hard_constraints["amount_minor"] = amount_minor
        elif hint_action == "compare_products" or (not hint_action and ("比较" in text or "推荐" in text or "哪个" in text)):
            goals.append("compare_products")
            facts["horizon_days"] = 365
            comparison_budget = budget_minor if budget_minor is not None else amount_minor
            if comparison_budget is None or comparison_budget <= 0:
                unresolved.append(_clarification("total_budget_minor", "请给出理财比较的含费预算。"))
            else:
                hard_constraints["total_budget_minor"] = comparison_budget
                evidence.append({"field": "hard_constraints.total_budget_minor", "value": comparison_budget,
                                 "span": budget_span or amount_span, "source": "user_message"})
        else:
            goals.append("purchase_wealth")
            resolved_product = resolve_product(session, text)
            if resolved_product["status"] == "resolved":
                product = resolved_product["product"]
                facts["product_id"] = product["product_id"]
                evidence.append({"field": "facts.product_id", "value": product["product_id"],
                                 "span": product["name"], "source": "user_message"})
            elif resolved_product["status"] == "ambiguous":
                unresolved.append(_clarification("product_id", "要申购哪一只产品？", resolved_product["candidates"]))
            if amount_minor is None and budget_minor is None:
                unresolved.append(_clarification("amount_minor", "请给出申购金额。"))
            else:
                hard_constraints["amount_minor"] = amount_minor or budget_minor
        if "波动" in text or "回撤" in text or "风险分析" in text:
            goals.append("quant_risk_report")

    elif scene == "S4_card":
        apply_words = any(word in text for word in ("申请", "办卡", "开卡", "办一张"))
        limit_verbs = any(word in text for word in ("提高", "提升", "调高", "降低", "调低", "调整", "修改", "增加", "减少"))
        # "申请…卡，额度 2000" 里的额度是申请参数，不是对已有卡调额；只有出现明确的调额动词才按调额处理。
        applying_with_limit = apply_words and "额度" in text and not limit_verbs
        if hint_action == "card_lost_report" or (not hint_action and ("挂失" in text or "丢了" in text or "解挂" in text)):
            goals.append("card_lost_report")
            _clarify_card(session, auth.user_id, text, facts, unresolved)
        elif hint_action == "card_replace" or (not hint_action and "补卡" in text):
            goals.append("card_replace")
            _clarify_card(session, auth.user_id, text, facts, unresolved)
        elif hint_action == "card_apply" or (not hint_action and apply_words and (applying_with_limit or "额度" not in text)):
            goals.append("card_apply")
            if amount_minor is not None:
                hard_constraints["requested_limit_minor"] = amount_minor
            facts["kind"] = "virtual" if "虚拟" in text else "physical"
        elif hint_action == "card_limit_change" or (not hint_action and "额度" in text and not applying_with_limit):
            goals.append("card_limit_change")
            _clarify_card(session, auth.user_id, text, facts, unresolved)
            if amount_minor is None:
                unresolved.append(_clarification("requested_limit_minor", "请给出希望调整到的额度（元）。"))
            else:
                hard_constraints["requested_limit_minor"] = amount_minor
            facts["direction"] = "increase" if any(k in text for k in ("提高", "提升", "调高", "增加")) else "decrease"
        elif hint_action == "card_control" or (not hint_action and ("限制" in text or "锁" in text)):
            goals.append("card_control")
            _clarify_card(session, auth.user_id, text, facts, unresolved)
        elif hint_action == "card_apply" or (not hint_action and any(word in text for word in ("申请", "办卡", "开卡"))):
            goals.append("card_apply")
        elif hint_action == "card_status" or (not hint_action and any(word in text for word in ("查询", "查看", "看看", "看下", "看一下"))):
            goals.append("card_status")
        else:
            unresolved.append(_clarification("card_action", "请明确卡片操作：查询、申请、额度调整、交易限制、挂失或补卡。"))
        if _is_vague(text):
            unresolved.append(_clarification("card_action", "请明确卡片操作：申请、额度调整、交易限制、挂失或补卡。"))

    elif scene == "S5_subscription":
        if hint_action == "cancel_subscription" or (not hint_action and any(k in text for k in ("取消", "退订", "别再扣"))):
            goals.append("cancel_subscription")
            goals.append("revoke_mandate")
            _clarify_subscription(session, auth.user_id, text, facts, unresolved)
            facts["note"] = "取消商户合同与解除银行代扣是两个独立动作，需分别核验结果"
        else:
            goals.append("list_subscriptions")
            facts["requires_confirmation"] = True

    elif scene == "S6_cross":
        goals.append("plan_event")
        if budget_minor is not None:
            hard_constraints["total_budget_minor"] = budget_minor
            evidence.append({"field": "hard_constraints.total_budget_minor", "value": budget_minor, "span": budget_span, "source": "user_message"})
        else:
            unresolved.append(_clarification("total_budget_minor", "请给出这次活动的总预算（含费用）。"))
        if when is not None:
            hard_constraints["delivery_deadline"] = format_iso(when)
            evidence.append({"field": "hard_constraints.delivery_deadline", "value": format_iso(when), "span": when_span, "source": "parsed_relative_date"})
        else:
            unresolved.append(_clarification("delivery_deadline", "请确认活动日期与时间。"))
        if "鲜花" in text or "花" in text:
            goals.append("book_flowers")
        if "蛋糕" in text:
            goals.append("book_cake")
        if "餐厅" in text or "聚餐" in text or "聚会" in text:
            goals.append("book_restaurant")
        if "aa" in text.lower() or "分摊" in text:
            goals.append("create_aa_request")
        if not any(g in goals for g in ("book_flowers", "book_cake", "book_restaurant")):
            unresolved.append(_clarification("event_services", "请选择需要预订的服务（鲜花、蛋糕、餐饮）。"))
        if "生日" in text and "买" not in text and "订" not in text and "预订" not in text:
            inferred.append(
                {
                    "field": "goals",
                    "value": ["book_flowers", "book_cake"],
                    "reason": "提到生日只触发候选提醒或规划，不会自动取得购买授权",
                }
            )
            unresolved.append(
                _clarification(
                    "confirm_purchase_intent",
                    "生日提醒已记录。是否需要我按预算准备鲜花与蛋糕的预订方案？（不会自动购买）",
                    options=[{"value": "yes", "label": "准备方案"}, {"value": "no", "label": "只要提醒"}],
                )
            )
        if "便宜" in text or "省钱" in text:
            preferences.append({"higher": "lower_cost", "lower": "delivery_speed"})
        if "快" in text or "尽快" in text:
            preferences.append({"higher": "delivery_speed", "lower": "lower_cost"})
        order_match = re.search(r"先(?:订|买|要)([\u4e00-\u9fa5]{2,4})", text)
        if order_match:
            preferences.append({"higher": order_match.group(1), "source": "user_priority"})

    else:
        # 没听懂时必须先问清楚：此前的非阻断澄清会让任务以"空计划"直接 blocked。
        unresolved.append(
            _clarification(
                "intent",
                "抱歉，我没理解你的需求。可以试试：转账、账单分析、理财、卡片、订阅或生日采购。",
                blocking=True,
            )
        )

    if _is_vague(text) and not unresolved:
        unresolved.append(
            _clarification("scope", "「都可以／你看着办」不能作为无限授权。请给出明确的金额、对象或时间范围。")
        )

    if model_clarification:
        unresolved.append(_clarification("intent", "暂时无法确定你的意图，请明确要查询还是办理什么业务。"))

    for account in snapshot["accounts"]:
        if account["account_id"] == default_account:
            facts["funding_snapshot"] = {
                "account_id": account["account_id"],
                "posted_balance_minor": account["posted_balance_minor"],
                "available_minor": account["available_minor"],
                "spendable_minor": account["spendable_minor"],
            }

    goal_spec = {
        "schema_version": SCHEMA_VERSION,
        "task_id": None,
        "revision": 1,
        "scene": scene,
        "matched_keywords": matched_keywords,
        "reference_time": format_iso(reference),
        "goals": sorted(set(goals)),
        "hard_constraints": hard_constraints,
        "preferences": preferences,
        "partial_completion_policy": "keep_successful_effects",
        "funding_sources": [default_account] if default_account else [],
        "unconfirmed_receivables_are_spendable": False,
        "unresolved": unresolved,
        "authorization_status": "not_requested",
        "facts": facts,
        "evidence": evidence,
        "inferred": inferred,
        "summary": build_summary(goals, hard_constraints, facts, preferences),
        "clarification_questions": [c["question"] for c in unresolved if c["blocking"]],
    }
    return goal_spec


def build_summary(goals: list[str], hard_constraints: dict, facts: dict, preferences: list[dict]) -> str:
    """结构化摘要：供用户核对，字段与后端校验同源。"""
    from ..money import format_minor

    parts: list[str] = []
    if "transfer" in goals:
        payee = (facts.get("payee_view") or {}).get("display_name", "（待确认收款人）")
        amount = hard_constraints.get("amount_minor")
        when = hard_constraints.get("execute_after")
        parts.append(f"向 {payee} 转账 {format_minor(amount) if amount else '（待确认金额）'} 元")
        if when:
            parts.append(f"执行时间 {when}")
    if "analyze_bill" in goals and facts.get("period"):
        period = facts["period"]
        parts.append(f"分析账单期间 {period.get('anchor') or period.get('start')}")
    if "plan_event" in goals:
        budget = hard_constraints.get("total_budget_minor")
        parts.append(f"活动预算 {format_minor(budget) if budget else '（待确认）'} 元")
        deadline = hard_constraints.get("delivery_deadline")
        if deadline:
            parts.append(f"截止 {deadline}")
    if "list_subscriptions" in goals:
        parts.append("查询订阅与代扣状态（需要确认）")
    if "cancel_subscription" in goals:
        parts.append("分别取消商户合同与解除银行代扣（两端结果独立）")
    if "card_lost_report" in goals:
        parts.append("挂失／解挂卡片（需要强验证）")
    floor = hard_constraints.get("protected_floor_minor")
    if floor:
        parts.append(f"应急金底线 {format_minor(floor)} 元")
    return "；".join(parts) if parts else "已记录需求，等待补充关键信息"


def merge_clarification_answer(goal_spec: dict, clarification_id: str, answer: object) -> dict:
    """把用户对澄清问题的回答并入 GoalSpec（结构化，不覆盖历史）。"""
    spec = dict(goal_spec)
    unresolved = [dict(c) for c in spec.get("unresolved", [])]
    target = next((c for c in unresolved if c.get("clarification_id") == clarification_id), None)
    if target is None:
        raise FinPilotError("PARAM_INVALID", "该澄清问题不存在或已回答。")
    target["answered"] = True
    target["answer"] = answer
    field_name = target.get("field")
    hard = dict(spec.get("hard_constraints", {}))
    facts = dict(spec.get("facts", {}))
    if field_name in ("amount_minor", "total_budget_minor", "requested_limit_minor", "final_cost_minor", "redeem_amount_minor"):
        try:
            minor = parse_amount_minor(answer) if not isinstance(answer, int) else int(answer)
        except FinPilotError:
            raise
        hard[field_name] = minor
    elif field_name == "payee_id":
        facts["payee_id"] = answer
        facts["payee_resolution"] = "resolved"
    elif field_name == "execute_after":
        if isinstance(answer, str) and answer != "other":
            hard["execute_after"] = answer
    elif field_name in ("delivery_deadline", "participants", "event_services", "card_action", "confirm_purchase_intent", "scope"):
        facts[field_name] = answer
    spec["hard_constraints"] = hard
    spec["facts"] = facts
    spec["unresolved"] = [c for c in unresolved if not (c.get("clarification_id") == clarification_id)]
    spec["summary"] = build_summary(spec.get("goals", []), hard, facts, spec.get("preferences", []))
    return spec


def blocking_clarifications(goal_spec: dict) -> list[dict]:
    return [c for c in goal_spec.get("unresolved", []) if c.get("blocking", True) and not c.get("answered")]
