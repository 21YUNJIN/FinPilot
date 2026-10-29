# 融策智能体 FinPilot：团队整合工作区

**状态：集成与审查中，尚未证明满足全部赛题要求。** 本项目使用合成数据、虚拟资金和本机模拟银行/商户，不接真实银行或证券交易。默认 `mock` 使用规则解析；可选 `live` 将脱敏后的合成输入发送到配置的模型，获取受限的场景和动作候选，再由服务端核验并执行授权流程。`offline_replay` 尚未实现。真实模型接入本系统的端到端效果尚待验证。

本目录把四人团队交付的 FinPilot、S3 银行理财风险服务和 StockQuant 统计内核放在一起。FinPilot 是比赛主体，量化服务仅计算银行理财产品净值风险，不能下单、代替产品官方风险等级、修改账户、签发授权或替代适当性评估。StockQuant 股票研究和交易模块不属于参赛主流程。

| 路径 | 用途 |
|---|---|
| `finpilot/` | API、六场景、两客户端、数据、测试及队伍原交付资料 |
| `services/quant-service/` | 独立只读 S3 风险 HTTP 服务及其原始测试 |
| `vendor/stockquant/` | S3 实际使用的 StockQuant 内核、原 LICENSE 及源码 |
| `docs/` | 需求基线、整合审查与后续缺口 |
| `evidence/` | 本机验证结果；需以文件日期与适用版本判断 |
| `scripts/` | 环境准备和本机模拟启动器 |

`finpilot/docs/` 内文件主要是 2026-10-27 原交付快照，可能描述旧接口、旧路径和旧测试结果；**不能直接当成本次整合的最终参赛材料**。原包来源及许可边界见 `docs/INTEGRATION_PROVENANCE.md`。

## Windows 安装与启动

需要 Python 3.12 64 位、PostgreSQL 17 和 PowerShell。原 FinPilot 离线 wheel 仅适配 Windows 64 位 Python 3.12；量化 Flask/NumPy/requests 以及 FinPilot 新增 requests 不在原离线包中，以下安装步骤需要网络。Node/pnpm 只在修改或重建前端时需要。

```powershell
cd <本目录>
powershell -ExecutionPolicy Bypass -File .\scripts\setup.ps1 -Python <Python3.12路径>
```

先由使用者准备**独立空的模拟演示数据库**，配置 `DATABASE_URL`。下列迁移与播种会写入所指数据库，不要指向已有生产或个人数据。种子不能视为任意非空数据库的安全重置方案。示例中的占位符必须替换：

```powershell
$env:DATABASE_URL = 'postgresql+psycopg://<用户名>:<密码>@127.0.0.1:<端口>/<独立演示库>'
$env:PYTHONPATH = (Join-Path (Get-Location) 'finpilot\services\api\src')
Push-Location .\finpilot
& ..\.venv-finpilot\Scripts\python.exe -m alembic upgrade head
& ..\.venv-finpilot\Scripts\python.exe -m finpilot.seed --demo
Pop-Location
& .\.venv-finpilot\Scripts\python.exe .\scripts\run_demo.py
```

启动器不创建、迁移、重置或播种数据库；使用独立的量化、银行和商户模拟进程，以本机回环地址运行。它会临时生成量化服务令牌，不写入源码；关闭前台进程使用 Ctrl+C。前端地址：`http://127.0.0.1:58000/app/`；IM：`http://127.0.0.1:58000/im/`。演示用户是种子里的 `user_demo_01`，密码见 `finpilot/services/api/src/finpilot/seed.py`，只用于合成数据演示。

演示环境的独立银行沙箱默认先受理，未收到或核验终态前应显示在途/未知；不得把受理当作已结算。用户授权、强验证和账本约束仍由 FinPilot 服务端决定。启动器依赖可用的 58000、58101、58102、8770 端口；端口占用时拒绝启动，不停止已有程序。

受理之后需要"机构侧处理完成"：真实银行会异步回调，本机沙箱则由控制面显式推进。演示时另开一个窗口执行：

```powershell
& .\.venv-finpilot\Scripts\python.exe .\scripts\settle_pending.py --dry-run   # 先看还有哪些在途
& .\.venv-finpilot\Scripts\python.exe .\scripts\settle_pending.py            # 推进并投递签名回调
```

脚本只处理 `bank_sandbox_v1` / `merchant_sandbox_v1` 的本机沙箱业务，且要求 `ENVIRONMENT=local`；它不直接改账本，资金效果仍由回调对账产生。任务终态需重新查询任务或客户端刷新后显示。

## 可选千问在线模型

此前粘贴到聊天中的密钥已暴露，请在模型服务控制台撤销并重新生成。新密钥不要发送到聊天、提交代码、保存到交付包或写入终端历史。先按上节准备独立模拟数据库，在启动器所在的同一个 PowerShell 窗口设置：

```powershell
$env:APP_MODE = 'live'
$env:MODEL_BASE_URL = 'https://maas.qianwenaiapi.com/compatible-mode/v1'
$env:MODEL_NAME = 'qwen3.8-max'
$secretInput = Read-Host '请输入新的模型 API 密钥（输入不显示）' -AsSecureString
$env:MODEL_API_KEY = [System.Net.NetworkCredential]::new('', $secretInput).Password
Remove-Variable secretInput
& .\.venv-finpilot\Scripts\python.exe .\scripts\run_demo.py
```

结束演示后使用 `Remove-Item Env:MODEL_API_KEY` 清除当前窗口中的密钥，`$env:APP_MODE = 'mock'` 可切回规则模式。调用模型可能产生费用，只使用合成案例；系统会做有限脱敏和注入检测，不能保证识别所有敏感信息。不要输入真实客户信息、密码或验证码。模型仅返回候选场景与动作，金额、收款对象和权限由服务端核对；模型失败或结果无效时停止，不会静默切换为 mock。`live` 是模型接入模式，银行和资金仍是模拟的。

## 量化结果与降级

FinPilot 把本项目合成理财产品的净值序列通过 HTTP 传给 S3。客户端核验请求编号、数据哈希、方法版本和指标单位；非净值产品、明显非日频或较大净值缺口、服务不可用、数据不足均返回 NA，不能当成低风险。历史波动、回撤和 CVaR 不代表未来收益或最大损失上限。FinPilot 自己保留的 `local_reference` 是对照实现，不能宣称是远程 StockQuant 调用。

## 待完成的竞赛工作

必须完成并记录：新密钥下真实模型在本系统的端到端测试和安全评测；六场景真实用户任务的端到端测试；独立沙箱全链路与异常恢复验证；Linux/容器的资源与系统调用隔离验证；多模型、基线、消融及留出集评测；最终材料的技术和安全版本更新。竞赛资格、报名回执、截止时间与组委会对模拟渠道/验证方式的认可还须团队核实。以上未完成前，不应声称全部合规或保证获奖。

赛事官方入口：[2026 深圳国际金融科技大赛](https://fintechathon.g-ican.com/)。具体规则与日期以报名系统及组委会最新通知为准。
