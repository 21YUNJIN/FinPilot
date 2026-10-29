# StockQuant 智能量化系统备份

- 备份时间：2026-10-28 17:08 CST
- 源目录：`/Users/Admin/Documents/Codex/2026-10-01/wo-x/work/stock-quant-mvp`
- 备份目录：`/Users/Admin/Documents/ChatGPT/银行/StockQuant_Backup_2026-10-28`
- 当前分支：`codex/finpilot-agent`
- HEAD：`5559e3fe12fefaa2055f6910dac38c3dfce9e68f`
- HEAD 提交：`5559e3f 增加人工执行网关与东方财富普通账户结论`

## 备份范围

已包含：

- `stockquant/` 完整代码；
- `bankagent/` 当前代码与文档；
- `tests/` 完整测试；
- `scripts/` 完整研究脚本；
- `docs/` 完整文档，包括 2026-10-28 技术说明与功能报告；
- `config/`；
- `data/state/`；
- `data/deploy_backups/`；
- `outputs/` 研究输出与报告；
- `demo_output/`；
- `README.md`、`pyproject.toml`、运行脚本和根目录交付文件；
- 当前工作区未提交 diff；
- 未跟踪文件清单；
- 完整 Git 历史 bundle；
- SHA-256 校验清单。

## 排除项

安全原因或体积原因未包含：

- `.venv/`；
- `data/real/`（约 5.5GB 原始行情数据）；
- `backups/`（历史备份目录）；
- `.git/`（由 `stockquant-history.bundle` 保存完整历史）；
- `__pycache__/`、`.pytest_cache/`、`*.pyc`；
- `.DS_Store`；
- `.env`；
- `.env.local`；
- `access_token.txt`；
- `*.key`、`*.pem`。

`data/real/` 中的原始行情数据仍保留在原仓库，或可按 `docs/FREE_DATA_SOURCE.md`、`docs/DATA_CONTRACT.md` 重新拉取。

## 恢复方式

1. 将本目录内容复制回任意工作目录；
2. 安装 Python 3.9+ 和所需依赖；
3. 如需完整 Git 历史：
   ```bash
   git clone stockquant-history.bundle stockquant-restored
   ```
4. 未提交改动可参考 `uncommitted.patch`；
5. 未跟踪文件已直接包含在备份目录中；
6. 原始大行情数据需要重新拉取或从原仓库复制 `data/real/`。

## 校验方式

在本目录执行：

```bash
shasum -a 256 -c SHA256SUMS
```

Git bundle 校验：

```bash
git bundle verify stockquant-history.bundle
```
