# Alembic 迁移链（migrations/）

对应 V5 需求 **DATA-04（数据库迁移链）、NFR-02（可部署与可恢复）、SYS-04（运行环境）**。

## 文件布局

| 文件 | 作用 |
|---|---|
| `../alembic.ini` | Alembic 配置（**仓库根目录**，因为 `python -m alembic` 从当前目录解析它）。**必须 ASCII-only**：Alembic 用 locale 编码（本机 cp936/GBK）读取 ini，中文注释会导致 `UnicodeDecodeError` |
| `env.py` | 连接解析 + autogenerate 口径：`target_metadata = finpilot.models.Base.metadata`、`compare_type=True`、`compare_server_default=False`；并显式把 `services/api/src` 插入 `sys.path`，未做 editable 安装的干净环境也能跑 |
| `script.py.mako` | 新修订模板（文件头写明"唯一事实来源是 `models.py`"与常用命令） |
| `versions/0001_initial.py` | **完整初始迁移**：autogenerate 自空库，含 47 张表 / 86 个索引 / 45 个外键 / 18 个唯一约束；文件头记录了生成命令、`models.py` 的 sha256 版本锚点与验收口径 |

## 连接解析顺序（`env.py`）

1. `-x db_url=postgresql+psycopg://...`（命令行覆盖，CI/脚本用）
2. `DATABASE_URL` 环境变量或仓库根 `.env`（经 `finpilot.config.get_settings()`）
3. `finpilot.config` 的默认值

配置文件里**不保存口令**：迁移链与运行中的应用永远使用同一份连接口径，避免"迁移建库 A、应用连库 B"。

## 常用命令

```powershell
$url = "postgresql+psycopg://finpilot:finpilot_dev_pw@127.0.0.1:55432/finpilot"

& .\.venv\Scripts\python.exe -m alembic -x db_url=$url upgrade head       # 建表/升级
& .\.venv\Scripts\python.exe -m alembic -x db_url=$url current            # -> 0001 (head)
& .\.venv\Scripts\python.exe -m alembic -x db_url=$url check              # 与 models.py 差异检查
& .\.venv\Scripts\python.exe -m alembic -x db_url=$url downgrade base     # 回滚到空库
& .\.venv\Scripts\python.exe -m alembic -x db_url=$url upgrade head --sql # 离线生成 SQL（不连库）
```

## 修改模型后的流程（强制）

1. 先改 `services/api/src/finpilot/models.py`（唯一事实来源）；
2. 生成**增量**修订（不要改历史修订、不要手写漏表）：

   ```powershell
   & .\.venv\Scripts\python.exe -m alembic -x db_url=$url revision --autogenerate -m "add xxx"
   ```

3. 人工复核生成的 `versions/<rev>_<slug>.py`（尤其 `server_default`、`ondelete`、唯一约束名）；
4. 跑验收：`alembic check` 必须输出 `No new upgrade operations detected.`，
   且 `tests/integration/test_migrations.py` 全绿（含"删表后 check 必须报错"的反向对照）。

## 验收口径（本机已实测）

```text
空库 -> upgrade head           -> 47 张业务表 + alembic_version，exit 0
alembic check                  -> No new upgrade operations detected.，exit 0
downgrade base                 -> 仅剩 alembic_version
再 upgrade head                -> 业务表 47（幂等）
关键唯一约束                    -> uq_operation_owner / uq_grant_use_operation /
                                  uq_daily_limit_bucket / uq_ledger_tx_operation_kind
pytest tests/integration/test_migrations.py tests/integration/test_backup_restore.py -q
                               -> 14 passed
```

## 边界与注意

* `downgrade` 不能修复"被带外改坏的库"：例如手工 `DROP TABLE merchant_quotes CASCADE` 后，
  `0001` 的 downgrade 会因 `op.drop_index('ix_merchant_quotes_merchant_id')` 找不到索引而失败。
  PostgreSQL 的 DDL 是事务性的，失败会整体回滚，不会留下半迁移状态；修复方式是
  `DROP SCHEMA public CASCADE; CREATE SCHEMA public;` 后重新 `upgrade head`。
* `compare_server_default=False` 是**有意选择**：`server_default=func.now()` 在反射时可能是
  `now()` 或 `CURRENT_TIMESTAMP`，开启会持续产生假差异。表结构一致性由
  "迁移建表集合 == `Base.metadata` 表集合" + `alembic check` 共同保证。
* 本机无 git 可执行文件，故 `0001_initial.py` 以 `models.py` 的 **sha256** 作为版本锚点
  （当前：`7d4a7901a77e1d2c3fd866dc37ecfb241af7fcb6846318d761247e43161f8786`）。
  改动 `models.py` 后该值必然变化，reviewer 可据此判断迁移是否需要同步更新。