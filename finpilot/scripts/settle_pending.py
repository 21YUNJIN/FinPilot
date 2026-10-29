"""演示用结算推进器：把仍"在途"的沙箱业务推进到终态并投递回调。

背景：银行/商户沙箱与真实机构一样**只受理、不主动回调**，结算由提供方侧推进
（``POST /bank/advance``、``POST /merchant/advance``）。现场演示需要一个显式的
"机构处理完成"动作，否则任务会一直显示在途。

用法（仅限本机模拟环境）::

    & .\\venv\\Scripts\\python.exe scripts\\settle_pending.py --dry-run   # 只列出在途业务
    & .\\venv\\Scripts\\python.exe scripts\\settle_pending.py            # 推进并投递回调

安全边界：
- 只处理 ``provider_id`` 为 ``bank_sandbox_v1`` / ``merchant_sandbox_v1`` 的本机沙箱业务；
- 需要 ``ENVIRONMENT=local``，非 local 直接拒绝；
- 不修改账本，只调用沙箱控制面；资金效果仍由 FinPilot 的商品化回调与对账产生。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import httpx
from sqlalchemy import select

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services" / "api" / "src"))

from finpilot.config import get_settings  # noqa: E402
from finpilot.db import session_scope  # noqa: E402
from finpilot.models import Operation  # noqa: E402

IN_FLIGHT = ("dispatching", "pending")
BANK_PROVIDER = "bank_sandbox_v1"
MERCHANT_PROVIDER = "merchant_sandbox_v1"


def pending_operations(session) -> list[dict]:
    rows = session.execute(
        select(Operation)
        .where(Operation.status.in_(IN_FLIGHT), Operation.provider_id.in_((BANK_PROVIDER, MERCHANT_PROVIDER)))
        .order_by(Operation.created_at)
    ).scalars().all()
    return [
        {
            "operation_id": row.operation_id,
            "tool": row.tool,
            "status": row.status,
            "provider_id": row.provider_id,
            "owner_user_id": row.owner_user_id,
            "amount_minor": row.amount_minor,
        }
        for row in rows
    ]


def advance(item: dict, *, client: httpx.Client, settle: bool, confirm: bool) -> dict:
    provider = item["provider_id"]
    if provider == BANK_PROVIDER:
        url = "/bank/advance"
        body = {"stable_operation_ref": item["operation_id"], "settle": settle, "emit_callback": True}
    else:
        url = "/merchant/advance"
        body = {
            "stable_operation_ref": item["operation_id"],
            "confirm": confirm,
            "settle": settle,
            "emit_callback": True,
        }
    response = client.post(url, json=body)
    try:
        payload = response.json()
    except ValueError:
        payload = {"raw": response.text[:200]}
    delivered = [bool(d.get("delivered")) for d in (payload.get("deliveries") or [])]
    return {
        "operation_id": item["operation_id"],
        "provider": provider,
        "http": response.status_code,
        "advanced": payload.get("advanced"),
        "deliveries": delivered,
        "failure_code": (payload.get("error") or {}).get("code"),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="推进本机沙箱在途业务并投递回调")
    parser.add_argument("--dry-run", action="store_true", help="只列出在途业务，不推进")
    parser.add_argument("--no-settle", action="store_true", help="只确认不结算（仅商户）")
    args = parser.parse_args(argv)

    settings = get_settings()
    if settings.environment != "local":
        print(f"拒绝执行：ENVIRONMENT={settings.environment!r}，本脚本只允许 local 模拟环境。", file=sys.stderr)
        return 2

    with session_scope() as session:
        items = pending_operations(session)

    if not items:
        print("没有在途的沙箱业务。")
        return 0

    print(f"在途业务 {len(items)} 条：")
    for item in items:
        print(f"  - {item['operation_id']}  tool={item['tool']}  provider={item['provider_id']}  status={item['status']}")

    if args.dry_run:
        print("\n--dry-run：未推进任何业务。")
        return 0

    results = []
    with httpx.Client(base_url=settings.bank_sandbox_base_url, timeout=30.0) as bank, httpx.Client(
        base_url=settings.merchant_sandbox_base_url, timeout=30.0
    ) as merchant:
        for item in items:
            client = bank if item["provider_id"] == BANK_PROVIDER else merchant
            try:
                results.append(advance(item, client=client, settle=not args.no_settle, confirm=True))
            except httpx.HTTPError as exc:
                results.append({"operation_id": item["operation_id"], "error": type(exc).__name__})

    print("\n推进结果：")
    ok = 0
    for result in results:
        if result.get("error"):
            print(f"  ✗ {result['operation_id']}: {result['error']}")
            continue
        delivered = all(result["deliveries"]) if result["deliveries"] else False
        ok += 1 if delivered else 0
        print(
            f"  {'✓' if delivered else '!'} {result['operation_id']}  http={result['http']}  "
            f"callbacks={result['deliveries']}  {result.get('failure_code') or ''}".rstrip()
        )
    print(f"\n完成 {ok}/{len(results)} 条（回调投递成功的条数）。任务终态由 FinPilot 的回调对账推进。")
    return 0 if ok == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())