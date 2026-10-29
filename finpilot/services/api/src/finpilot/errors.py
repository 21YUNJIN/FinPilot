"""统一错误码、HTTP 语义与安全消息（V5 16.2）。

规则：
- 业务状态以信封为准，HTTP 200 不等于交易成功。
- safe_message 面向用户，不含堆栈、SQL、密钥，也不泄露其他用户对象是否存在。
- retry_mode 告诉调用方是否可以安全重试、是否需要查询原操作。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# --- 重试方式 ---
RETRY_NEVER = "never"
RETRY_SAFE_QUERY = "safe_query"          # 只读查询可重试
RETRY_QUERY_SAME_OPERATION = "query_same_operation"  # 必须用原 operation_id 查询
RETRY_SAME_OPERATION = "retry_same_operation"        # 同一幂等键重试
RETRY_AFTER_BACKOFF = "retry_after_backoff"
RETRY_AFTER_REAUTH = "after_reauthorization"
RETRY_AFTER_REPAIR = "after_input_fix"


@dataclass(frozen=True)
class ErrorSpec:
    http_status: int
    retry_mode: str
    safe_message: str


# 错误码 → (HTTP 语义, 重试方式, 安全消息)
ERROR_SPECS: dict[str, ErrorSpec] = {
    "PARAM_INVALID": ErrorSpec(400, RETRY_AFTER_REPAIR, "请求参数不合法，请修正后重试。"),
    "AMOUNT_PRECISION": ErrorSpec(400, RETRY_AFTER_REPAIR, "金额最多两位小数，请重新输入。"),
    "SCHEMA_INVALID": ErrorSpec(400, RETRY_AFTER_REPAIR, "请求结构与约定的 Schema 不一致。"),
    "UNKNOWN_TOOL": ErrorSpec(400, RETRY_AFTER_REPAIR, "该能力未在工具目录中登记，不能执行。"),
    "EXTRA_DANGEROUS_FIELD": ErrorSpec(400, RETRY_AFTER_REPAIR, "请求包含不允许的额外字段。"),
    "UNAUTHENTICATED": ErrorSpec(401, RETRY_AFTER_REAUTH, "登录状态无效，请重新登录。"),
    "FORBIDDEN": ErrorSpec(403, RETRY_NEVER, "当前身份没有执行该操作的权限。"),
    "OBJECT_NOT_ACCESSIBLE": ErrorSpec(403, RETRY_NEVER, "无法访问该对象。"),
    "POLICY_DENIED": ErrorSpec(403, RETRY_NEVER, "服务端策略不允许执行该操作。"),
    "GRANT_REQUIRED": ErrorSpec(403, RETRY_NEVER, "该操作需要用户明确确认。"),
    "GRANT_INVALID": ErrorSpec(403, RETRY_NEVER, "授权无效或已失效。"),
    "GRANT_SCOPE_VIOLATION": ErrorSpec(403, RETRY_NEVER, "该操作超出已授权范围，需要重新确认。"),
    "CHALLENGE_EXPIRED": ErrorSpec(403, RETRY_AFTER_REAUTH, "验证已过期，请重新发起验证。"),
    "CHALLENGE_FAILED": ErrorSpec(403, RETRY_AFTER_REAUTH, "验证码不正确。"),
    "CHALLENGE_LOCKED": ErrorSpec(429, RETRY_AFTER_BACKOFF, "验证尝试次数过多，请稍后再试。"),
    "FACTOR_NOT_ENROLLED": ErrorSpec(403, RETRY_AFTER_REAUTH, "尚未配置强验证因子，无法完成该操作。"),
    "VERSION_CONFLICT": ErrorSpec(409, RETRY_AFTER_REPAIR, "对象已被其他操作更新，请刷新后重试。"),
    "IDEMPOTENCY_CONFLICT": ErrorSpec(409, RETRY_NEVER, "同一幂等键被用于不同参数，已拒绝。"),
    "NOT_REVERSIBLE": ErrorSpec(409, RETRY_NEVER, "该操作按条款不可撤销。"),
    "CANCEL_NOT_EFFECTIVE": ErrorSpec(409, RETRY_NEVER, "撤销请求未生效，原操作仍按实际结果处理。"),
    "INSUFFICIENT_FUNDS": ErrorSpec(422, RETRY_AFTER_REPAIR, "可用余额不足。"),
    "PROTECTED_FLOOR_VIOLATION": ErrorSpec(422, RETRY_AFTER_REPAIR, "该操作会穿透已确认的应急金底线。"),
    "RESERVATION_EXCEEDED": ErrorSpec(422, RETRY_AFTER_REPAIR, "预留金额超过该账户可用余额。"),
    "RISK_MISMATCH": ErrorSpec(422, RETRY_AFTER_REPAIR, "该产品与当前风险测评结果不匹配。"),
    "ASSESSMENT_REQUIRED": ErrorSpec(422, RETRY_AFTER_REPAIR, "需要先完成有效的风险测评。"),
    "INFEASIBLE": ErrorSpec(422, RETRY_AFTER_REPAIR, "在现有硬约束下没有可行方案。"),
    "PLANNING_LIMIT": ErrorSpec(422, RETRY_AFTER_REPAIR, "规划达到工程限额而中止，不代表任务无解。"),
    "PROVIDER_REJECTED": ErrorSpec(422, RETRY_NEVER, "业务提供方拒绝了该请求。"),
    "RATE_LIMITED": ErrorSpec(429, RETRY_AFTER_BACKOFF, "请求过于频繁，请稍后重试。"),
    "PROVIDER_TIMEOUT": ErrorSpec(503, RETRY_QUERY_SAME_OPERATION, "请求结果尚未确认，正在查询原订单。"),
    "PROVIDER_UNAVAILABLE": ErrorSpec(503, RETRY_SAME_OPERATION, "业务提供方暂时不可用，未产生资金效果。"),
    "PROVIDER_CAPABILITY_MISSING": ErrorSpec(503, RETRY_NEVER, "提供方不支持稳定业务键或结果查询，已关闭自动写入。"),
    "RESULT_UNKNOWN": ErrorSpec(503, RETRY_QUERY_SAME_OPERATION, "结果未知，正在用原操作编号核对，请勿重复提交。"),
    "SANDBOX_UNAVAILABLE": ErrorSpec(503, RETRY_NEVER, "分析沙箱不可用，已关闭生成代码执行。"),
    "SANDBOX_VIOLATION": ErrorSpec(422, RETRY_NEVER, "生成代码触发了沙箱禁止能力，已阻止执行。"),
    "AUDIT_UNAVAILABLE": ErrorSpec(503, RETRY_AFTER_BACKOFF, "审计记录不可写，高风险新提交已暂停。"),
    "CIRCUIT_OPEN": ErrorSpec(503, RETRY_AFTER_BACKOFF, "连续失败触发熔断，冷却后仅允许有限探测。"),
    "SAFETY_LOCKED": ErrorSpec(429, RETRY_AFTER_BACKOFF, "检测到可疑行为，高风险操作已暂时锁定。"),
    "CAPABILITY_NOT_SUPPORTED": ErrorSpec(422, RETRY_AFTER_REPAIR, "当前版本不支持该业务能力。"),
    "DEPENDENCY_FAILED": ErrorSpec(503, RETRY_AFTER_BACKOFF, "依赖的离线或缓存能力失败，未伪造结果。"),
    "CLARIFICATION_REQUIRED": ErrorSpec(409, RETRY_AFTER_REPAIR, "关键信息未确定，需要先澄清。"),
    "SYSTEM_ERROR": ErrorSpec(500, RETRY_AFTER_BACKOFF, "系统内部错误，未产生资金效果。"),
}


@dataclass
class FinPilotError(Exception):
    """带错误码、HTTP 语义和安全消息的领域异常。"""

    code: str
    message: str | None = None
    details: dict[str, Any] = field(default_factory=dict)
    http_status: int | None = None
    retry_mode: str | None = None

    def __post_init__(self) -> None:
        spec = ERROR_SPECS.get(self.code)
        if spec is None:
            spec = ERROR_SPECS["SYSTEM_ERROR"]
        if self.http_status is None:
            self.http_status = spec.http_status
        if self.retry_mode is None:
            self.retry_mode = spec.retry_mode
        Exception.__init__(self, f"{self.code}: {self.message or spec.safe_message}")

    @property
    def safe_message(self) -> str:
        spec = ERROR_SPECS.get(self.code, ERROR_SPECS["SYSTEM_ERROR"])
        return self.message or spec.safe_message

    def to_error_object(self) -> dict[str, Any]:
        obj: dict[str, Any] = {
            "code": self.code,
            "safe_message": self.safe_message,
            "retry_mode": self.retry_mode,
        }
        if self.details:
            # 只放可公开的结构化原因码，不放内部堆栈
            obj["reason_codes"] = self.details.get("reason_codes", [])
            extra = {
                k: v
                for k, v in self.details.items()
                if k != "reason_codes" and isinstance(v, (str, int, float, bool, list, dict))
            }
            if extra:
                obj["details"] = extra
        return obj


def unknown_code(code: str) -> bool:
    return code not in ERROR_SPECS