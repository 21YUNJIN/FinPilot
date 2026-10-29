# FinPilot 前端工作区（apps/）

两个独立客户端 + 共享 UI 组件库，连接同一后端（`services/api`）与同一账号体系。

| 包 | 说明 | dev 端口 |
|---|---|---|
| `apps/shared-ui` | 共享组件、HTTP 客户端、状态标签、错误码镜像、hooks（两个客户端唯一的前端事实来源） | — |
| `apps/web` | APP 式客户端（移动优先）：顶部模式徽标 + 底部标签导航（对话/任务/卡片/账单/理财/订阅/隐私） | 5173 |
| `apps/im` | IM 风格客户端：左侧会话列表 + 右侧消息流，气泡内嵌授权卡片与回执卡片 | 5174 |

## 运行

```powershell
# 依赖（本机 node/pnpm 路径见下）
& "<node.exe>" "<pnpm.mjs>" install
& "<node.exe>" "<pnpm.mjs>" -r typecheck
& "<node.exe>" "<pnpm.mjs>" -r test
& "<node.exe>" "<pnpm.mjs>" -r build
& "<node.exe>" "<pnpm.mjs>" --filter @finpilot/web dev   # APP 客户端
& "<node.exe>" "<pnpm.mjs>" --filter @finpilot/im dev    # IM 客户端
```

后端地址：`VITE_API_BASE_URL`（缺省 `http://127.0.0.1:58000`）。
可在 `apps/web/.env.local` / `apps/im/.env.local` 覆盖（`.gitignore` 已忽略）。

## 契约来源

- `contracts/openapi.yaml`、`contracts/schemas/*.json`：请求/响应结构与状态枚举；
- `docs/INTERFACES.md`：信封、金额/时间/ID 口径、写操作必须带 `expected_revision` 与幂等键；
- `services/api/src/finpilot/errors.py`：错误码 → 用户文案（前端在
  `apps/shared-ui/src/api/errorMessages.ts` 维护**只读镜像**，并由
  `tests/error-mirror.parity.test.ts` 直接读取 Python 源码校验一致性，漂移即测试失败）；
- `services/api/src/finpilot/models.py`：状态常量（前端只做展示映射）。

## 前端硬规则（实现与测试都覆盖）

1. HTTP 200 但 `status !== "ok"` 一律当失败（`api/client.ts`）。
2. 写操作按接口选择幂等键字段（`operation_id` / `idempotency_key` / `client_message_id`），
   并复用同一键重试；`additionalProperties:false` 的接口不会被塞入未登记字段。
3. 401 清除会话与用户级缓存；切换用户清空缓存（另一个用户读不到上一个用户的数据）。
4. `VERSION_CONFLICT`：提示刷新、保留已确认动作、不用旧修订覆盖服务端结论。
5. RED 未完成强验证时**不显示提交按钮、不出现成功字样**；验证码只走独立请求，
   不进入对话、日志或页面文本。
6. 状态一律文字化（`StatusChip`），风险不只用颜色；金额一律整数分（BigInt 格式化，禁浮点）；
   缺失数据显示"不适用"；不使用任何假进度动画或"演示成功"。
7. 运行模式（`live` / `offline_replay` / `mock`）始终标注，缺失时显示"未标注（不得当作实时运行）"。

## 与契约的已知假设（需要与 B/D 对齐）

| 位置 | 假设 | 影响 |
|---|---|---|
| `TaskView.messages` | 契约 `task_view.json` 未定义消息数组；前端按展示需要读取 | 若网关不内联，需要消息接口（IM 与 APP 的对话区都是可选展示） |
| `TaskView.grants` / `challenge` | 同上，为展示补充字段 | 授权卡片状态由 `plan.required_level` + 该字段推导 |
| `WaitingReason.since` / `action_hint` | 契约只有 `code`/`safe_message`/`operation_id`/`grant_id`/`challenge_id` | 缺失时界面不显示这两行 |
| `ReceiptOutcome` | 由 `OperationView`（+`Receipt`）推导的展示口径，非契约对象 | 推导规则与测试在 `api/adapters.ts` / `operation-receipt.test.tsx` |
| `HandoffCase`、`PrivacyExportView`、`RetentionRule` | 契约暂无 schema（A05 交付物） | 人工接管/隐私面板按客户端展示结构实现 |
| `experimentalApi.*` | `/v1/accounts/snapshot`、`/v1/aa/requests`、`/v1/wealth/assessment`、`/v1/auth/me` 未在 OpenAPI 登记 | 未实现时界面显示真实错误（`INVALID_ENVELOPE`），不伪造数据 |
| 卡片申请状态 | `card.json` 无申请字段（另有 `card_application.json`） | 界面按"任务视图中未返回申请对象"显示 |
| 订阅代扣状态 | `mandate.json` 是独立对象 | 组件支持内联 `subscription.mandate` 或外部 `mandates` 列表 |

## 测试

- `apps/shared-ui/tests`：22 个文件 / 133 个用例（含错误码一致性、HTTP200 但失败、RED 未验证、
  scope_hash、状态文字化、整数分金额、版本冲突、换用户清缓存、NA、部分成功/未知状态）；
- `apps/web/tests`、`apps/im/tests`：集成测试（登录闸门 → 导航 → 真实数据 → 无假进度）。