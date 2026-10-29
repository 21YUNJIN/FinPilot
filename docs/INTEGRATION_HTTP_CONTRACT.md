# 整合版 HTTP 接口补充

本文件记录本次新增或改变的接口；原 `finpilot/contracts/openapi.yaml` 是冻结的旧版子集，尚未覆盖下列所有运行接口。运行服务的 `/openapi.json` 由 FastAPI 生成；在提交材料前仍需由团队合并和复核完整静态契约、权限标注和响应 Schema，不能声称旧静态契约已覆盖全部路径。

业务接口需要通过 `Authorization: Bearer <登录令牌>` 访问。`status` 和 `error` 决定业务结果，HTTP 200 不能单独视为任务成功。请求和日志不可含真实金融账户或凭证；以下仅用于模拟环境。

| 方法 / 路径 | 实际行为 | 权限/边界 |
|---|---|---|
| `GET /v1/subscriptions` | 返回空 `items` 与 `requires_confirmation: true` | 确认前不返回合同详情 |
| `POST /v1/subscriptions/query` | 请求 `confirmed: true`、唯一 `operation_id`；成功后返回用户自己的列表 | 人类确认入口，模型不可调用；服务端签发并消费精确 YELLOW 授权，同编号幂等 |
| `GET /v1/wealth/assessment` | 有有效测评时返回等级、版本与有效期；无有效测评返回 `data: null` | 当前用户只读；失效测评不能用于购买适当性 |
| `GET /v1/wealth/comparison` | 参数 `budget_minor`（分）、`horizon_days`、`preference`（risk/yield/fee/liquidity）；返回银行约束与可用的 `quant_risk` | 先做银行条款/预算/风险约束；服务不可用不伪造零风险 |
| `POST /v1/quant/risk-report`（独立 S3） | 接收产品目录、净值序列和截至日，返回逐产品统计与哈希 | 服务间令牌，独立只读端口；并非用户授权或申购接口 |

APP 和 IM 的静态资源分别位于 `/app/` 和 `/im/`。独立银行与商户模拟服务只供本地联调用，不代表任何真实银行授权或清算。
