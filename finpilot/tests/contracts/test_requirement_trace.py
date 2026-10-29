"""92 项 V5 需求追溯：解析、校验并生成 `docs/requirement-trace.md`（V5 GOV-01 / T60 / DEL-01）。

数据来源（仓库内自包含）：
- `docs/BANKAGENT_TEAM4_PLAN.md` 附录「92 项 V5 需求到工作包的完整映射」：需求 ID、主责、主工作包、验收测试族；
- `docs/BANKAGENT_SRS_V5_COMPETITION.md` 各章需求表：需求 ID、验收测试、主责（用于交叉复核主责与测试族）。

本模块是纯静态校验，不连接数据库（覆盖 `tests/conftest.py` 的自动 `clean_db` 夹具）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.contract

REPO_ROOT = Path(__file__).resolve().parents[2]
DOCS = REPO_ROOT / "docs"
PLAN_PATH = DOCS / "BANKAGENT_TEAM4_PLAN.md"
SRS_PATH = DOCS / "BANKAGENT_SRS_V5_COMPETITION.md"
TRACE_PATH = DOCS / "requirement-trace.md"
CONTRACTS = REPO_ROOT / "contracts"

OWNERS = ("A", "B", "C", "D")
EXPECTED_OWNER_COUNTS = {"A": 22, "B": 26, "C": 22, "D": 22}
EXPECTED_TOTAL = 92
EXPECTED_WORK_PACKAGES = {f"{owner}{index:02d}" for owner in OWNERS for index in range(1, 7)}
FAMILY_MIN, FAMILY_MAX = 1, 60

PLAN_ROW = re.compile(
    r"^\|\s*(?P<rid>[A-Z][A-Z0-9]*-\d{2})\s*\|(?P<text>.*?)\|\s*(?P<owner>[ABCD])\s*\|"
    r"\s*(?P<wp>[ABCD]\d{2})\s*\|\s*(?P<fams>.*?)\s*\|\s*$"
)
FAMILY = re.compile(r"T\d{2}")
ID_CELL = re.compile(r"^[A-Z][A-Z0-9]*-\d{2}$")


@dataclass(frozen=True)
class Requirement:
    rid: str
    owner: str
    work_package: str
    families: tuple[str, ...]
    text: str


@pytest.fixture(autouse=True)
def clean_db():
    """故意为空：追溯校验只读文档，不需要数据库。"""
    yield


# --------------------------------------------------------------------------
# 解析
# --------------------------------------------------------------------------
def parse_plan_appendix() -> list[Requirement]:
    text = PLAN_PATH.read_text(encoding="utf-8")
    marker = "## 附录"
    assert marker in text, "TEAM4 计划缺少附录需求映射表"
    appendix = text[text.index(marker):]
    rows: list[Requirement] = []
    for line in appendix.splitlines():
        match = PLAN_ROW.match(line.strip())
        if not match:
            continue
        families = tuple(dict.fromkeys(FAMILY.findall(match.group("fams"))))
        rows.append(
            Requirement(
                rid=match.group("rid"),
                owner=match.group("owner"),
                work_package=match.group("wp"),
                families=families,
                text=re.sub(r"\s+", " ", match.group("text")).strip(),
            )
        )
    return rows


def parse_srs_requirements() -> dict[str, tuple[str, tuple[str, ...]]]:
    """SRS 各章需求表：| 需求 ID | 必须实现的行为 | 验收测试 | 主责 |。"""
    out: dict[str, tuple[str, tuple[str, ...]]] = {}
    for line in SRS_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if len(cells) != 4 or not ID_CELL.match(cells[0]) or cells[3] not in OWNERS:
            continue
        families = tuple(dict.fromkeys(FAMILY.findall(cells[2])))
        out[cells[0]] = (cells[3], families)
    return out


def load_manifest_work_packages() -> set[str]:
    manifest = yaml.safe_load((CONTRACTS / "module-manifest.yaml").read_text(encoding="utf-8"))
    return {wp for module in manifest["modules"] for wp in module.get("work_packages", [])}


PLAN_ROWS = parse_plan_appendix()
SRS_ROWS = parse_srs_requirements()


# --------------------------------------------------------------------------
# 校验
# --------------------------------------------------------------------------
def test_appendix_has_92_unique_requirements():
    ids = [row.rid for row in PLAN_ROWS]
    assert len(ids) == EXPECTED_TOTAL, f"附录应含 {EXPECTED_TOTAL} 项需求，实际 {len(ids)}"
    duplicates = sorted({rid for rid in ids if ids.count(rid) > 1})
    assert not duplicates, f"需求 ID 重复：{duplicates}"


def test_every_requirement_has_owner_work_package_and_test_family():
    for row in PLAN_ROWS:
        assert row.owner in OWNERS, f"{row.rid} 的主责非法：{row.owner}"
        assert re.match(r"^[ABCD]\d{2}$", row.work_package), f"{row.rid} 的主工作包非法：{row.work_package}"
        assert row.work_package[0] == row.owner, f"{row.rid} 的主责 {row.owner} 与工作包 {row.work_package} 不一致"
        assert row.work_package in EXPECTED_WORK_PACKAGES, f"{row.rid} 的工作包不在 24 个工作包内"
        assert row.families, f"{row.rid} 缺少验收测试族"
        assert row.text, f"{row.rid} 缺少需求描述"


def test_test_families_are_within_t01_t60():
    for row in PLAN_ROWS:
        for family in row.families:
            index = int(family[1:])
            assert FAMILY_MIN <= index <= FAMILY_MAX, f"{row.rid} 引用了范围外的测试族 {family}"


def test_owner_workload_matches_team_plan():
    counts = {owner: 0 for owner in OWNERS}
    for row in PLAN_ROWS:
        counts[row.owner] += 1
    assert counts == EXPECTED_OWNER_COUNTS, f"主责分布与计划 §1 不一致：{counts}"


def test_all_work_packages_are_used():
    used = {row.work_package for row in PLAN_ROWS}
    assert used == EXPECTED_WORK_PACKAGES, f"未被任何需求使用的（或越界的）工作包：{sorted(used ^ EXPECTED_WORK_PACKAGES)}"


def test_plan_mapping_is_consistent_with_srs():
    assert len(SRS_ROWS) == EXPECTED_TOTAL, f"SRS 应含 {EXPECTED_TOTAL} 项需求，实际 {len(SRS_ROWS)}"
    plan_ids = {row.rid for row in PLAN_ROWS}
    assert set(SRS_ROWS) == plan_ids, (
        f"计划附录与 SRS 的需求 ID 集合不一致：仅计划={sorted(plan_ids - set(SRS_ROWS))}；"
        f"仅 SRS={sorted(set(SRS_ROWS) - plan_ids)}"
    )
    for row in PLAN_ROWS:
        srs_owner, srs_families = SRS_ROWS[row.rid]
        assert srs_owner == row.owner, f"{row.rid} 主责与 SRS 不一致：{row.owner} != {srs_owner}"
        assert set(srs_families) == set(row.families), (
            f"{row.rid} 验收测试族与 SRS 不一致：{row.families} != {srs_families}"
        )


def test_requirement_work_packages_are_registered_in_module_manifest():
    registered = load_manifest_work_packages()
    missing = sorted({row.work_package for row in PLAN_ROWS} - registered)
    assert not missing, f"以下主工作包未在 contracts/module-manifest.yaml 中登记：{missing}"


# --------------------------------------------------------------------------
# 生成 docs/requirement-trace.md
# --------------------------------------------------------------------------
TRACE_HEADER = """# 需求追溯矩阵（92 项 V5 需求 → 主责 → 工作包 → 测试族）

本文件由 `tests/contracts/test_requirement_trace.py` 依据
`docs/BANKAGENT_TEAM4_PLAN.md` 附录「92 项 V5 需求到工作包的完整映射」生成并校验，
同时与 `docs/BANKAGENT_SRS_V5_COMPETITION.md` 各章需求表交叉复核主责与验收测试族。

**「当前实现状态」列故意留空**：它必须由各工作包按 V5 附录 B 的证据格式登记
（`work_package_id, requirement_ids, test_case_id, commit, dataset_hash, model_version, seed,
initial_snapshot, expected, actual, result, evidence_paths, reviewer`），
允许取值 `planned` / `in_progress` / `verified` / `not_run`。
空单元格**不等于通过**，也不代表已测试；禁止在没有运行证据的情况下填写 `verified`。

- 需求总数：{total}（A {a} / B {b} / C {c} / D {d}）
- 主工作包：24 个，全部被使用；测试族编号范围 T01—T60
- 生成来源：`docs/BANKAGENT_TEAM4_PLAN.md` 附录（唯一权威映射）
"""


def render_trace(rows: list[Requirement]) -> str:
    counts = {owner: sum(1 for row in rows if row.owner == owner) for owner in OWNERS}
    lines = [
        TRACE_HEADER.format(total=len(rows), a=counts["A"], b=counts["B"], c=counts["C"], d=counts["D"]).rstrip(),
        "",
        "## 1 追溯表",
        "",
        "| 需求 ID | 主责 | 主工作包 | 验收测试族 | 当前实现状态 | V5 要求（摘自 TEAM4 附录） |",
        "|---|---|---|---|---|---|",
    ]
    for row in rows:
        text = row.text.replace("|", "/")
        families = "、".join(row.families)
        lines.append(f"| {row.rid} | {row.owner} | {row.work_package} | {families} |  | {text} |")

    lines += ["", "## 2 按主责统计", "", "| 主责 | 需求数 | 工作包 |", "|---|---|---|"]
    for owner in OWNERS:
        packages = sorted({row.work_package for row in rows if row.owner == owner})
        lines.append(f"| {owner} | {counts[owner]} | {'、'.join(packages)} |")

    lines += ["", "## 3 按工作包统计", "", "| 工作包 | 主责 | 需求数 | 需求 ID |", "|---|---|---|---|"]
    for package in sorted(EXPECTED_WORK_PACKAGES):
        package_rows = [row for row in rows if row.work_package == package]
        ids = "、".join(row.rid for row in package_rows)
        lines.append(f"| {package} | {package[0]} | {len(package_rows)} | {ids} |")

    lines += ["", "## 4 测试族覆盖面", "", "| 测试族 | 引用需求 |", "|---|---|"]
    for index in range(FAMILY_MIN, FAMILY_MAX + 1):
        family = f"T{index:02d}"
        ids = "、".join(row.rid for row in rows if family in row.families)
        lines.append(f"| {family} | {ids if ids else '（本版本未被任何需求直接引用）'} |")

    return "\n".join(lines) + "\n"


def test_generate_requirement_trace():
    """生成（或刷新）docs/requirement-trace.md；实现状态列留空待登记。"""
    content = render_trace(PLAN_ROWS)
    TRACE_PATH.write_text(content, encoding="utf-8")
    assert TRACE_PATH.exists()
    assert "当前实现状态" in content


def test_requirement_trace_contains_every_requirement():
    assert TRACE_PATH.exists(), "docs/requirement-trace.md 必须存在（由上一个测试生成）"
    text = TRACE_PATH.read_text(encoding="utf-8")
    for row in PLAN_ROWS:
        assert f"| {row.rid} | {row.owner} | {row.work_package} |" in text, f"{row.rid} 未出现在追溯表中"
        for family in row.families:
            assert family in text
    body = [line for line in text.splitlines() if line.startswith("| ") and ID_CELL.match(line.split("|")[1].strip())]
    assert len(body) == EXPECTED_TOTAL, f"追溯表应含 {EXPECTED_TOTAL} 行需求，实际 {len(body)}"


def test_requirement_trace_leaves_status_column_empty():
    text = TRACE_PATH.read_text(encoding="utf-8")
    table = text[text.index("## 1 追溯表"):text.index("## 2 按主责统计")]
    for line in table.splitlines():
        cells = [c.strip() for c in line.strip("|").split("|")]
        if len(cells) >= 5 and ID_CELL.match(cells[0]):
            assert cells[4] == "", f"{cells[0]} 的实现状态列必须留空待登记，当前为 {cells[4]!r}"
            assert "verified" not in cells[4]