# 本次整合来源与许可说明

截至 2026-10-29：原始交付在 `D:/12122121`，原始包及哈希清单在 `evidence/source_archives.json`，原文件未在整合时修改。FinPilot 普通包和离线包的共同业务文件比对一致；本整合选择离线包作为 FinPilot 基线。S3 与 StockQuant 分别来自团队提供的独立压缩包和备份。

| 来源 | 本次用途 | 核实状态 |
|---|---|---|
| FinPilot | 后端、前端、业务领域、原始文档与测试 | 队伍交付；项目整体权属和参赛成员授权需团队核实 |
| S3_QUANT_SERVICE | S3 净值风险服务和 HTTP 客户端 | 原包文档声明 MIT；需团队核对全部新增贡献者授权 |
| StockQuant | 通过 S3 实际导入 `stockquant.backtest.metrics` | 原备份包含 MIT `LICENSE`，保留在 `vendor/stockquant/LICENSE`；许可证文件已查验，但源代码权利链和所有贡献者授权仍需团队核实 |
| 种子数据 | 合成产品、账户、流水和评测数据 | 仓库声明全部金融数据为合成；不能冒充真实客户、产品或银行数据 |
| Python、Node 依赖 | 各服务运行和构建 | 每个依赖的发行包与许可应在最终提交前逐项清点，遵守各自条件 |

`finpilot/data/metadata/source_manifest.json` 及 `finpilot/docs/` 保留的是原团队交付快照，其中“StockQuant 待核验/未复制”等说法不代表本整合后的状态。本文件是本次代码装配的补充说明，不能替代完整知识产权和第三方许可审查。

量化算法方法口径以 `services/quant-service/docs/METHOD_CARD.md` 和远程响应的 `method_version` 为准。S3 的评分式排序和压力测试作为服务保留；当前 FinPilot 比较页主要使用 S3 风险报告，产品硬过滤与偏好排序仍由银行侧实施，不能声称所有 S3 端点已进入前端。
