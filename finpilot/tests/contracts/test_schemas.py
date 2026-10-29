"""契约包测试：Schema、正反例、工具目录、OpenAPI、模块注册表（V5 API-01 / SYS-05 / GOV-01）。

本模块是**纯静态契约校验**：只读 `contracts/`、`docs/INTERFACES.md` 与
`services/api/src/finpilot/{models,errors}.py`，不连接数据库、不启动服务。
因此这里覆盖了 `tests/conftest.py` 的自动 `clean_db` 夹具（它需要真实 PostgreSQL），
让契约测试可以在任何环境下独立运行；其余测试目录仍按 V5 第 6 节跑真实 PostgreSQL。
"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path

import pytest
import yaml
from jsonschema import Draft202012Validator
from referencing import Registry, Resource

pytestmark = pytest.mark.contract

REPO_ROOT = Path(__file__).resolve().parents[2]
CONTRACTS = REPO_ROOT / "contracts"
SCHEMA_DIR = CONTRACTS / "schemas"
EXAMPLE_DIR = CONTRACTS / "examples"
DOCS = REPO_ROOT / "docs"
SRC = REPO_ROOT / "services" / "api" / "src" / "finpilot"
SCHEMA_BASE = "https://finpilot.local/schemas/"
DRAFT_2020_12 = "https://json-schema.org/draft/2020-12/schema"

# 交付要求中"至少覆盖"的 Schema 清单（V5 第 4.2 节契约表 + API-01）。
REQUIRED_SCHEMAS = [
    "auth_context", "task", "task_view", "goal", "clarification", "plan", "plan_step", "policy_decision",
    "snapshot", "snapshot_account", "operation_draft", "operation_view", "grant", "grant_use", "auth_challenge",
    "domain_result", "result_view", "error_envelope", "money", "card", "card_application", "card_control",
    "bill_report", "wealth_product", "risk_profile", "wealth_order", "subscription", "mandate", "reminder",
    "merchant_quote", "merchant_order", "aa_request", "aa_share", "provider_event", "receipt", "audit_event",
    "tool_catalog_entry",
]
# 对外输入面：必须 additionalProperties=false 且禁止客户端覆盖服务端字段。
INPUT_SCHEMAS = ["task", "operation_draft", "grant_request", "tool_request"]
DANGEROUS_FIELD_VALUES = {
    "user_id": "user_demo_02",
    "owner_user_id": "user_demo_02",
    "task_id": "task_injected",
    "status": "succeeded",
    "revision": 99,
    "plan_id": "plan_injected",
    "grant_id": "grant_injected",
    "required_level": "GREEN",
    "verification_level": "NONE",
    "challenge_id": "chl_injected",
    "scope": {"tool": "transfer.submit", "amount_minor": 900000},
    "scope_kind": "scoped",
    "max_uses": 99,
    "max_total_minor": 900000,
    "approved_revision": 99,
    "roles": ["operator"],
    "is_operator": True,
    "mode": "live",
    "posted_balance_minor": 999999999,
    "available_minor": 999999999,
    "parameters_hash": "0" * 64,
    "scope_hash": "0" * 64,
    "provider_order_ref": "bank_ord_forged",
    "effect_confirmed": True,
    "settled_at": "2026-10-22T10:00:00+08:00",
    "version": 99,
    "goal_spec": {"goals": ["bypass"]},
    "result_summary": {"succeeded": True},
}

# 枚举常量 → (Schema 文件, JSON 指针)。测试用 AST 解析 models.py 后逐项比对。
MODELS_TUPLE_ENUMS = [
    ("TASK_STATUSES", "task_view", "/properties/status"),
    ("TASK_STATUSES", "result_view", "/properties/task_status"),
    ("OPERATION_STATUSES", "operation_view", "/properties/status"),
    ("GRANT_STATUSES", "grant", "/properties/status"),
    ("AUTH_LEVELS", "grant", "/properties/required_level"),
    ("AUTH_LEVELS", "plan", "/properties/required_level"),
    ("AUTH_LEVELS", "plan_step", "/properties/required_level"),
    ("AUTH_LEVELS", "policy_decision", "/properties/required_level"),
    ("CARD_LIFECYCLE", "card", "/properties/lifecycle_status"),
    ("CARD_APPLICATION_STATUS", "card_application", "/properties/application_status"),
    ("SUBSCRIPTION_STATUS", "subscription", "/properties/status"),
    ("MANDATE_STATUS", "mandate", "/properties/status"),
    ("WEALTH_ORDER_STATUS", "wealth_order", "/properties/status"),
]
MODELS_CHECK_CONSTRAINT_ENUMS = [
    ("ck_accounts_status", "snapshot_account", "/properties/status"),
    ("ck_tasks_status", "task_view", "/properties/status"),
    ("ck_operations_status", "operation_view", "/properties/status"),
    ("ck_grants_status", "grant", "/properties/status"),
    ("ck_grants_scope_kind", "grant", "/properties/scope_kind"),
    ("ck_challenge_status", "auth_challenge", "/properties/status"),
    ("ck_card_lifecycle", "card", "/properties/lifecycle_status"),
    ("ck_card_application_status", "card_application", "/properties/application_status"),
    ("ck_subscription_evidence", "subscription", "/properties/evidence"),
]
# 时间字段命名约定：以 _at 结尾（时间戳）或以 _date 结尾（业务日）。
DATETIME_KEY_EXTRAS = {"reference_time", "timestamp", "delivery_deadline"}
PASSTHROUGH_KEYWORDS = (
    "items", "allOf", "anyOf", "oneOf", "not", "if", "then", "else", "additionalProperties",
    "patternProperties", "propertyNames", "contains", "prefixItems", "$defs",
)


# --------------------------------------------------------------------------
# 夹具：覆盖 tests/conftest.py 的数据库夹具（契约测试是静态校验）
# --------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def clean_db():
    """故意为空：契约测试不触碰数据库，也不需要 TEST_DATABASE_URL。"""
    yield


# --------------------------------------------------------------------------
# 载入与遍历工具
# --------------------------------------------------------------------------
def load_schemas() -> dict[str, dict]:
    return {p.stem: json.loads(p.read_text(encoding="utf-8")) for p in sorted(SCHEMA_DIR.glob("*.json"))}


def load_tool_catalog() -> dict:
    return yaml.safe_load((CONTRACTS / "tool-catalog.yaml").read_text(encoding="utf-8"))


def load_openapi() -> dict:
    return yaml.safe_load((CONTRACTS / "openapi.yaml").read_text(encoding="utf-8"))


def build_registry(schemas: dict[str, dict]) -> Registry:
    return Registry().with_resources(
        [(SCHEMA_BASE + name + ".json", Resource.from_contents(schema)) for name, schema in schemas.items()]
    )


def validator_for(name: str, schemas: dict[str, dict]) -> Draft202012Validator:
    return Draft202012Validator(
        schemas[name], registry=build_registry(schemas), format_checker=Draft202012Validator.FORMAT_CHECKER
    )


def iter_schema_nodes(node, pointer=""):
    """遍历 Schema 中所有子 Schema 节点，产出 (指针, 节点)。"""
    if not isinstance(node, dict):
        return
    yield pointer, node
    props = node.get("properties")
    if isinstance(props, dict):
        for key, sub in props.items():
            yield from iter_schema_nodes(sub, f"{pointer}/properties/{key}")
    for keyword in PASSTHROUGH_KEYWORDS:
        sub = node.get(keyword)
        if isinstance(sub, dict):
            yield from iter_schema_nodes(sub, f"{pointer}/{keyword}")
        elif isinstance(sub, list):
            for idx, item in enumerate(sub):
                yield from iter_schema_nodes(item, f"{pointer}/{keyword}/{idx}")


def iter_definition_nodes(node, pointer=""):
    """只遍历"定义"节点（properties / items / $defs），跳过 allOf / if-then 等收窄分支。

    收窄分支本来就允许把字段进一步限定（例如 NA 时 nav_e6 必须为 null），
    命名与类型约定只对定义处做机械校验。
    """
    if not isinstance(node, dict):
        return
    props = node.get("properties")
    if isinstance(props, dict):
        for key, sub in props.items():
            sub_pointer = f"{pointer}/properties/{key}"
            yield sub_pointer, sub
            if isinstance(sub, dict):
                yield from iter_definition_nodes(sub.get("items"), f"{sub_pointer}/items")
                yield from iter_definition_nodes(sub, sub_pointer)
    defs = node.get("$defs")
    if isinstance(defs, dict):
        for key, sub in defs.items():
            yield f"{pointer}/$defs/{key}", sub
            yield from iter_definition_nodes(sub, f"{pointer}/$defs/{key}")
    if isinstance(node.get("items"), dict):
        yield from iter_definition_nodes(node["items"], f"{pointer}/items")
    for keyword in ("additionalProperties", "patternProperties"):
        sub = node.get(keyword)
        if isinstance(sub, dict):
            yield from iter_definition_nodes(sub, f"{pointer}/{keyword}")


def collect_refs(node) -> list[str]:
    refs: list[str] = []
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "$ref" and isinstance(value, str):
                refs.append(value)
            else:
                refs.extend(collect_refs(value))
    elif isinstance(node, list):
        for item in node:
            refs.extend(collect_refs(item))
    return refs


def type_set(node: dict) -> set[str]:
    value = node.get("type")
    if isinstance(value, str):
        return {value}
    if isinstance(value, list):
        return {v for v in value if isinstance(v, str)}
    return set()


def module_constants(path: Path) -> dict[str, object]:
    """AST 读取模块顶层可字面求值的常量（不导入 SQLAlchemy 模型）。"""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    out: dict[str, object] = {}
    for node in tree.body:
        target = value = None
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            target, value = node.target.id, node.value
        elif isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            target, value = node.targets[0].id, node.value
        if target is None or value is None:
            continue
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            out[target] = value.value
            continue
        if isinstance(value, (ast.Tuple, ast.List)):
            try:
                out[target] = list(ast.literal_eval(value))
            except Exception:  # noqa: BLE001 - 只关心字面常量
                continue
    return out


def dict_keys(path: Path, name: str) -> list[str]:
    """读取模块顶层字典字面量的键（值可能含函数调用，不做字面求值）。"""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in tree.body:
        target = value = None
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            target, value = node.target.id, node.value
        elif isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            target, value = node.targets[0].id, node.value
        if target == name and isinstance(value, ast.Dict):
            return [ast.literal_eval(k) for k in value.keys]
    raise AssertionError(f"{path} 中找不到字典常量 {name}")


def check_constraint_enums(path: Path) -> dict[str, list[str]]:
    """从模型文件的 CheckConstraint("field in (...)") 提取字段枚举（与 DB 约束逐字一致）。"""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    result: dict[str, list[str]] = {}
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "CheckConstraint"):
            continue
        if not node.args:
            continue
        try:
            sql = ast.literal_eval(node.args[0])
        except Exception:  # noqa: BLE001
            continue
        match = re.search(r"(\w+) in \(([^)]*)\)", sql)
        if not match:
            continue
        field, values = match.group(1), re.findall(r"'([^']*)'", match.group(2))
        for keyword in node.keywords:
            if keyword.arg == "name" and isinstance(keyword.value, ast.Constant):
                result[keyword.value.value] = (field, values)  # type: ignore[assignment]
    return result  # type: ignore[return-value]


def resolve_pointer(schema: dict, pointer: str):
    node = schema
    for part in pointer.strip("/").split("/"):
        if not part:
            continue
        node = node[part]
    return node


def interfaces_section(heading: str) -> str:
    text = (DOCS / "INTERFACES.md").read_text(encoding="utf-8")
    start = text.index(heading)
    rest = text[start + len(heading):]
    nxt = rest.find("\n## ")
    return rest[:nxt] if nxt != -1 else rest


def interfaces_http_operations() -> set[tuple[str, str]]:
    """解析 INTERFACES.md 第 4 节 markdown 表格 → {(METHOD, path)}。"""
    ops: set[tuple[str, str]] = set()
    methods = {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}
    for line in interfaces_section("## 4 HTTP 契约").splitlines():
        line = line.strip()
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if len(cells) < 2 or cells[0].upper() not in methods:
            continue
        row_method = cells[0].upper()
        for token in cells[1].replace("`", "").split("、"):
            token = token.strip().rstrip("…").strip()
            if not token:
                continue
            parts = token.split()
            if len(parts) == 2 and parts[0].upper() in methods:
                ops.add((parts[0].upper(), parts[1]))
            elif token.startswith("/"):
                ops.add((row_method, token))
    return ops


def interfaces_tool_names() -> set[str]:
    """解析 INTERFACES.md 第 5 节工具清单。"""
    return set(re.findall(r"`([a-z_]+\.[a-z_]+)`", interfaces_section("## 5 工具目录")))


def srs_tool_names() -> set[str]:
    """展开 SRS 第 16.1 节的紧凑写法（transfer.prepare/submit/status）。"""
    text = (DOCS / "BANKAGENT_SRS_V5_COMPETITION.md").read_text(encoding="utf-8")
    marker = "业务工具目录至少包含："
    segment = text[text.index(marker) + len(marker):]
    segment = segment.split("。")[0]
    names: set[str] = set()
    for token in re.split(r"[、\s]+", segment.strip()):
        token = token.strip()
        if not token:
            continue
        if "/" not in token:
            names.add(token)
            continue
        head, *suffixes = token.split("/")
        prefix = head.split(".")[0]
        names.add(head)
        for suffix in suffixes:
            names.add(f"{prefix}.{suffix}")
    return names


def openapi_operations(spec: dict):
    for path, item in spec["paths"].items():
        for method, operation in item.items():
            if method.lower() in {"get", "post", "put", "patch", "delete", "head", "options"}:
                yield method.upper(), path, operation


def required_fields(schema: dict) -> set[str]:
    return set(schema.get("required", []))


def json_schema_ref(spec: dict, ref: str) -> dict:
    rel = ref.replace("schemas/", "")
    return json.loads((SCHEMA_DIR / rel).read_text(encoding="utf-8"))


# --------------------------------------------------------------------------
# 1 Schema 结构与 $id
# --------------------------------------------------------------------------
def test_required_schema_coverage():
    names = set(load_schemas())
    missing = [n for n in REQUIRED_SCHEMAS if n not in names]
    assert not missing, f"缺少必需 Schema：{missing}"
    for extra in ("grant_request", "tool_request"):
        assert extra in names, f"缺少对外输入面 Schema：{extra}"


def test_schema_files_are_valid_2020_12():
    for name, schema in load_schemas().items():
        assert schema["$schema"] == DRAFT_2020_12, f"{name}.json 的 $schema 必须是 2020-12"
        assert schema["$id"] == SCHEMA_BASE + name + ".json", f"{name}.json 的 $id 与文件名不一致"
        assert schema.get("title") and schema.get("description"), f"{name}.json 缺少 title/description"
        Draft202012Validator.check_schema(schema)


def test_schema_ids_are_unique_and_refs_resolvable():
    schemas = load_schemas()
    ids = [schema["$id"] for schema in schemas.values()]
    assert len(ids) == len(set(ids)), "存在重复 $id"
    registry = build_registry(schemas)
    resolver = registry.resolver()
    for name, schema in schemas.items():
        for ref in collect_refs(schema):
            resolver.lookup(ref)  # 无法解析会抛 Unresolvable / NoSuchAnchor
            assert ref.startswith(SCHEMA_BASE), f"{name}.json 引用了非契约目录的 $ref：{ref}"


# --------------------------------------------------------------------------
# 2 正反例
# --------------------------------------------------------------------------
def test_every_schema_has_positive_and_negative_example():
    schemas = load_schemas()
    missing = []
    for name in schemas:
        for kind in ("positive", "negative"):
            if not (EXAMPLE_DIR / f"{name}.{kind}.json").exists():
                missing.append(f"{name}.{kind}.json")
    assert not missing, f"缺少样例文件：{missing}"


def test_examples_do_not_embed_explanations():
    """反例必须是干净载荷：说明写在 contracts/examples/README.md，不能污染校验。"""
    for path in EXAMPLE_DIR.glob("*.json"):
        text = path.read_text(encoding="utf-8")
        assert "__why_invalid" not in text, f"{path.name} 内嵌了说明字段"
        payload = json.loads(text)
        if isinstance(payload, dict):
            assert "$comment" not in payload and "x-why-invalid" not in payload, f"{path.name} 内嵌了说明字段"


def example_schema_name(path: Path) -> str:
    """把样例文件名还原为 Schema 名：<name>.positive.json / <name>.negative.json / <name>.negative.<slug>.json。"""
    return path.name.split(".", 1)[0]


def extra_negative_examples() -> list[Path]:
    return sorted(p for p in EXAMPLE_DIR.glob("*.negative.*.json"))


def test_examples_readme_documents_every_negative():
    readme = (EXAMPLE_DIR / "README.md").read_text(encoding="utf-8")
    negatives = sorted(list(EXAMPLE_DIR.glob("*.negative.json")) + extra_negative_examples())
    for path in negatives:
        assert path.name in readme, f"{path.name} 未在 contracts/examples/README.md 中说明拒绝原因"


@pytest.mark.parametrize("name", sorted(load_schemas()))
def test_positive_examples_are_valid(name: str):
    schemas = load_schemas()
    example = json.loads((EXAMPLE_DIR / f"{name}.positive.json").read_text(encoding="utf-8"))
    errors = sorted(validator_for(name, schemas).iter_errors(example), key=lambda e: list(e.path))
    assert not errors, f"{name}.positive.json 未通过校验：{[e.message for e in errors]}"


@pytest.mark.parametrize("name", sorted(load_schemas()))
def test_negative_examples_are_rejected(name: str):
    schemas = load_schemas()
    example = json.loads((EXAMPLE_DIR / f"{name}.negative.json").read_text(encoding="utf-8"))
    errors = list(validator_for(name, schemas).iter_errors(example))
    assert errors, f"{name}.negative.json 竟然通过了校验，反例失效"


@pytest.mark.parametrize("path", extra_negative_examples(), ids=lambda p: p.name)
def test_extra_negative_examples_are_rejected(path: Path):
    """补充反例（如负数金额、浮点余额）：同样必须真的被拒绝。"""
    schemas = load_schemas()
    name = example_schema_name(path)
    assert name in schemas, f"{path.name} 找不到对应 Schema"
    example = json.loads(path.read_text(encoding="utf-8"))
    errors = list(validator_for(name, schemas).iter_errors(example))
    assert errors, f"{path.name} 竟然通过了校验，反例失效"


@pytest.mark.parametrize("name", INPUT_SCHEMAS)
def test_input_schemas_forbid_dangerous_fields(name: str):
    schemas = load_schemas()
    schema = schemas[name]
    assert schema.get("additionalProperties") is False, f"{name}.json 必须是 additionalProperties=false"
    forbidden = schema.get("x-forbidden-client-fields", [])
    for required_field in ("user_id", "owner_user_id", "grant_id", "status"):
        assert required_field in forbidden, f"{name}.json 未显式禁止 {required_field}"
    assert "拒绝" in schema["description"], f"{name}.json 的 description 必须写明拒绝原因"

    validator = validator_for(name, schemas)
    base = json.loads((EXAMPLE_DIR / f"{name}.positive.json").read_text(encoding="utf-8"))
    assert not list(validator.iter_errors(base))
    for field in forbidden:
        payload = dict(base)
        payload[field] = DANGEROUS_FIELD_VALUES.get(field, "injected")
        assert list(validator.iter_errors(payload)), f"{name}.json 未拒绝被禁止字段 {field}"


# --------------------------------------------------------------------------
# 3 金额、份额、时间口径
# --------------------------------------------------------------------------
def test_no_number_types_anywhere():
    offenders = []
    for name, schema in load_schemas().items():
        for pointer, node in iter_schema_nodes(schema):
            if "number" in type_set(node):
                offenders.append(f"{name}.json{pointer}")
    assert not offenders, f"契约禁止 number（浮点）类型：{offenders}"


def test_money_share_and_nav_fields_are_integer():
    offenders = []
    for name, schema in load_schemas().items():
        for pointer, node in iter_definition_nodes(schema):
            if "/properties/" not in pointer:
                continue
            key = pointer.rsplit("/", 1)[-1]
            if not (key.endswith("_minor") or key.endswith("_e8") or key.endswith("_e6") or key == "fee_bps"):
                continue
            if not ({"integer"} <= type_set(node)):
                offenders.append(f"{name}.json{pointer} type={node.get('type')}")
    assert not offenders, f"金额／份额／净值字段必须是整数（分、1e-8、1e-6 定点）：{offenders}"


def test_time_fields_use_iso_formats():
    offenders = []
    for name, schema in load_schemas().items():
        for pointer, node in iter_definition_nodes(schema):
            if "/properties/" not in pointer:
                continue
            key = pointer.rsplit("/", 1)[-1]
            expects_dt = key.endswith("_at") or key in DATETIME_KEY_EXTRAS
            expects_date = key.endswith("_date")
            if not (expects_dt or expects_date):
                continue
            expected = "date-time" if expects_dt else "date"
            if type_set(node) - {"null"} != {"string"} or node.get("format") != expected:
                offenders.append(f"{name}.json{pointer} type={node.get('type')} format={node.get('format')}")
    assert not offenders, f"时间字段必须是 {{type: string, format: date-time|date}}：{offenders}"


# --------------------------------------------------------------------------
# 4 枚举与错误码必须与实现一致
# --------------------------------------------------------------------------
def test_enums_match_models_constants():
    constants = module_constants(SRC / "models.py")
    schemas = load_schemas()
    mismatches = []
    for constant, schema_name, pointer in MODELS_TUPLE_ENUMS:
        expected = constants[constant]
        actual = resolve_pointer(schemas[schema_name], pointer)["enum"]
        if list(actual) != list(expected):
            mismatches.append(f"{schema_name}.json{pointer} != models.{constant}")
    assert not mismatches, f"枚举与 models.py 常量不一致：{mismatches}"


def test_enums_match_models_check_constraints():
    constraints = check_constraint_enums(SRC / "models.py")
    schemas = load_schemas()
    mismatches = []
    for constraint, schema_name, pointer in MODELS_CHECK_CONSTRAINT_ENUMS:
        assert constraint in constraints, f"models.py 中找不到约束 {constraint}"
        _field, expected = constraints[constraint]
        actual = resolve_pointer(schemas[schema_name], pointer)["enum"]
        if list(actual) != list(expected):
            mismatches.append(f"{schema_name}.json{pointer} != models.{constraint}")
    assert not mismatches, f"枚举与 models.py CheckConstraint 不一致：{mismatches}"


def test_error_codes_and_retry_modes_match_errors_module():
    constants = module_constants(SRC / "errors.py")
    codes = dict_keys(SRC / "errors.py", "ERROR_SPECS")
    defs = load_schemas()["error_envelope"]["$defs"]
    assert list(defs["error_code"]["enum"]) == list(codes), "error_envelope.error.code 与 errors.ERROR_SPECS 不一致"
    retry = [
        constants["RETRY_NEVER"],
        constants["RETRY_SAFE_QUERY"],
        constants["RETRY_QUERY_SAME_OPERATION"],
        constants["RETRY_SAME_OPERATION"],
        constants["RETRY_AFTER_BACKOFF"],
        constants["RETRY_AFTER_REAUTH"],
        constants["RETRY_AFTER_REPAIR"],
    ]
    assert list(defs["retry_mode"]["enum"]) == retry, "error_envelope.error.retry_mode 与 errors.RETRY_* 不一致"


# --------------------------------------------------------------------------
# 5 工具目录
# --------------------------------------------------------------------------
CATALOG_REQUIRED_FIELDS = [
    "name", "category", "read_write", "default_level", "agent_exposed", "idempotent", "timeout_seconds",
    "input_schema", "output_schema", "preconditions", "effects", "compensation", "sensitive_fields",
    "error_codes", "implemented_by",
]


def test_tool_catalog_entries_are_complete():
    catalog = load_tool_catalog()
    tools = catalog["tools"]
    schemas = load_schemas()
    codes = set(dict_keys(SRC / "errors.py", "ERROR_SPECS"))
    for tool in tools:
        missing = [f for f in CATALOG_REQUIRED_FIELDS if f not in tool]
        assert not missing, f"{tool.get('name')} 缺少字段：{missing}"
        assert tool["read_write"] in {"read", "write"}
        assert tool["default_level"] in {"GREEN", "YELLOW", "RED", "OPERATOR"}, f"{tool['name']} 未分级"
        assert isinstance(tool["agent_exposed"], bool)
        assert isinstance(tool["idempotent"], bool)
        assert 1 <= tool["timeout_seconds"] <= 120
        assert tool["preconditions"], f"{tool['name']} 必须声明前置条件"
        assert tool["effects"], f"{tool['name']} 必须声明效果"
        assert tool["compensation"]["mode"] in {"none", "auto", "manual", "within_authorized_scope"}
        assert tool["compensation"]["description"], f"{tool['name']} 必须声明补偿条件"
        assert tool["error_codes"], f"{tool['name']} 必须声明错误码"
        unknown = [c for c in tool["error_codes"] if c not in codes]
        assert not unknown, f"{tool['name']} 使用了未登记错误码：{unknown}"
        for key in ("input_schema", "output_schema"):
            ref = tool[key]
            assert ref.startswith(SCHEMA_BASE), f"{tool['name']}.{key} 必须指向契约 Schema"
            schema_name = ref.rsplit("/", 1)[-1][: -len(".json")]
            assert schema_name in schemas, f"{tool['name']}.{key} 指向不存在的 Schema：{schema_name}"
        assert re.match(r"^services/[A-Za-z0-9_\-/]+\.py$", tool["implemented_by"]), tool["implemented_by"]


def test_tool_catalog_write_tools_are_graded_and_operator_tools_are_not_exposed():
    tools = load_tool_catalog()["tools"]
    for tool in tools:
        if tool["read_write"] == "write":
            assert tool["default_level"] in {"GREEN", "YELLOW", "RED", "OPERATOR"}, f"{tool['name']} 是未分级写操作"
            assert tool["level_rule"], f"写工具 {tool['name']} 必须给出等级规则"
            assert tool["idempotent"] is True, f"写工具 {tool['name']} 必须支持幂等（operation_id）"
        else:
            assert tool["idempotent"] is True, f"读工具 {tool['name']} 必须可安全重试"
        if tool["default_level"] == "OPERATOR":
            assert tool["agent_exposed"] is False, f"运营工具 {tool['name']} 不得暴露给模型"


def test_tool_catalog_covers_interfaces_and_srs():
    names = {t["name"] for t in load_tool_catalog()["tools"]}
    interfaces = interfaces_tool_names()
    assert len(interfaces) == 33, f"INTERFACES §5 解析出 {len(interfaces)} 个工具，预期 33"
    assert interfaces == srs_tool_names(), "INTERFACES §5 与 SRS 第 16.1 节的工具清单不一致"
    assert interfaces <= names, f"工具目录缺少：{sorted(interfaces - names)}"
    for extra in names - interfaces:
        entry = next(t for t in load_tool_catalog()["tools"] if t["name"] == extra)
        assert entry["default_level"] == "OPERATOR" and entry["agent_exposed"] is False, (
            f"{extra} 不在 INTERFACES §5 清单内，只能是 OPERATOR + agent_exposed=false 的运营工具"
        )


def test_transfer_prepare_and_submit_are_restricted():
    tools = {t["name"]: t for t in load_tool_catalog()["tools"]}
    prepare_text = " ".join(tools["transfer.prepare"]["effects"] + tools["transfer.prepare"]["preconditions"])
    assert "不产生外部支付" in prepare_text, "transfer.prepare 必须写明不产生外部支付"
    for line in tools["transfer.prepare"]["effects"]:
        if "不产生外部支付" in line:
            continue
        assert not any(word in line for word in ("支付", "扣款", "记账", "分录", "余额减少")), (
            f"transfer.prepare 的效果不得包含资金动作：{line}"
        )

    submit = tools["transfer.submit"]
    submit_text = " ".join(submit["preconditions"] + submit["effects"])
    assert "只接受已登记并获授权的 operation 引用" in submit_text, "transfer.submit 必须限定为已登记操作引用"
    assert "不接受模型任意新拼装" in submit_text, "transfer.submit 必须拒绝模型新拼装参数"
    assert submit["default_level"] == "YELLOW"
    assert "100000" in submit["level_rule"], "transfer.submit 必须写明 100000 分的 YELLOW/RED 边界"
    assert "手续费不计入本金累计" in submit["level_rule"]

    assert tools["subscription.list"]["default_level"] == "YELLOW", "订阅查询是官方黄色例外，不能按绿色处理"


def test_write_tool_enum_matches_catalog():
    catalog_names = {
        t["name"] for t in load_tool_catalog()["tools"] if t["read_write"] == "write" and t["default_level"] != "OPERATOR"
    }
    schemas = load_schemas()
    for name in ("operation_draft", "tool_request"):
        found = None
        for branch in schemas[name]["allOf"]:
            enum = branch.get("if", {}).get("properties", {}).get("tool", {}).get("enum")
            if enum:
                found = set(enum)
                break
        assert found is not None, f"{name}.json 必须用 if-then 约束写工具"
        assert found == catalog_names, f"{name}.json 的写工具集合与工具目录不一致：{sorted(found ^ catalog_names)}"


# --------------------------------------------------------------------------
# 6 OpenAPI
# --------------------------------------------------------------------------
def test_openapi_is_3_1_and_paths_match_interfaces_section4():
    spec = load_openapi()
    assert spec["openapi"].startswith("3.1"), "契约要求 OpenAPI 3.1"
    actual = {(method, path) for method, path, _ in openapi_operations(spec)}
    expected = interfaces_http_operations()
    assert len(expected) == 26, f"INTERFACES §4 解析出 {len(expected)} 条路径，预期 26"
    assert actual == expected, (
        f"OpenAPI 路径集合与 INTERFACES §4 不一致；缺少={sorted(expected - actual)}；多余={sorted(actual - expected)}"
    )


def test_openapi_operation_ids_are_unique_snake_case():
    spec = load_openapi()
    ids = []
    for _method, _path, operation in openapi_operations(spec):
        op_id = operation.get("operationId")
        assert op_id, "每个 operation 必须有 operationId"
        assert re.match(r"^[a-z][a-z0-9_]*$", op_id), f"operationId 必须是 snake_case：{op_id}"
        ids.append(op_id)
    assert len(ids) == len(set(ids)), "operationId 必须唯一"


def test_openapi_error_responses_reference_error_envelope():
    spec = load_openapi()
    for method, path, operation in openapi_operations(spec):
        responses = operation.get("responses", {})
        assert responses, f"{method} {path} 缺少响应定义"
        refs = [json.dumps(resp) for resp in responses.values()]
        assert any("error_envelope.json" in ref for ref in refs), f"{method} {path} 的错误响应必须引用 error_envelope"
        success = responses.get("200")
        assert success is not None, f"{method} {path} 缺少 200 响应"


BUSINESS_WRITE_ENDPOINTS = {
    ("POST", "/v1/tasks/{task_id}/messages"),
    ("POST", "/v1/tasks/{task_id}/plan"),
    ("POST", "/v1/tasks/{task_id}/cancel"),
    ("POST", "/v1/grants/{grant_id}/revoke"),
    ("POST", "/v1/tasks/{task_id}/resume"),
    ("POST", "/v1/privacy/delete"),
    ("POST", "/operator/cases/{case_id}/actions"),
}
WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


def _body_required(operation: dict) -> set[str]:
    body = operation.get("requestBody", {})
    schema = body.get("content", {}).get("application/json", {}).get("schema", {})
    if "$ref" in schema:
        schema = json_schema_ref(load_openapi(), schema["$ref"])
    return required_fields(schema)


def test_openapi_write_operations_require_operation_id_and_revision():
    spec = load_openapi()
    for method, path, operation in openapi_operations(spec):
        if method not in WRITE_METHODS:
            continue
        if operation.get("x-requires-operation-id") is True:
            required = _body_required(operation)
            assert {"operation_id", "expected_revision"} <= required, (
                f"{method} {path} 标注了 x-requires-operation-id，请求体必须要求 operation_id 与 expected_revision"
            )
        else:
            exempt = operation.get("x-idempotency-exempt")
            assert isinstance(exempt, str) and len(exempt) > 8, (
                f"{method} {path} 是写操作：要么 x-requires-operation-id=true，要么给出 x-idempotency-exempt 幂等说明"
            )
        if (method, path) in BUSINESS_WRITE_ENDPOINTS:
            assert operation.get("x-requires-operation-id") is True, f"{method} {path} 是业务写操作，必须带 operation_id"


def test_openapi_agent_exposure_and_required_levels():
    spec = load_openapi()
    allowed_levels = {"GREEN", "YELLOW", "RED", "OPERATOR", "NONE"}
    expectations = {
        ("POST", "/v1/plans/{plan_id}/confirm"): "YELLOW",
        ("POST", "/v1/auth/challenges/{challenge_id}/verify"): "RED",
        ("GET", "/v1/subscriptions"): "YELLOW",
        ("GET", "/operator/cases"): "OPERATOR",
        ("POST", "/operator/cases/{case_id}/actions"): "OPERATOR",
        ("POST", "/internal/provider-events/{provider}"): "OPERATOR",
    }
    for method, path, operation in openapi_operations(spec):
        level = operation.get("x-required-level")
        assert level in allowed_levels, f"{method} {path} 的 x-required-level 非法：{level}"
        assert isinstance(operation.get("x-agent-exposed"), bool), f"{method} {path} 缺少 x-agent-exposed"
        if path.startswith("/operator") or path.startswith("/internal"):
            assert operation["x-agent-exposed"] is False, f"{method} {path} 不得暴露给模型"
        if path.startswith("/v1/auth/") or path.startswith("/v1/privacy/"):
            assert operation["x-agent-exposed"] is False, f"{method} {path} 属人类专用端点"
        if (method, path) in expectations:
            assert level == expectations[(method, path)], f"{method} {path} 等级应为 {expectations[(method, path)]}"
        if method == "GET" and path not in {"/v1/subscriptions", "/v1/privacy/export",
                                           "/operator/cases", "/v1/meta/tools"}:
            assert level == "GREEN", f"纯查询 {method} {path} 应为 GREEN"


def test_openapi_provider_callback_is_adapter_only():
    spec = load_openapi()
    for method, path, operation in openapi_operations(spec):
        if path == "/internal/provider-events/{provider}":
            assert operation["x-agent-exposed"] is False
            assert operation["x-required-level"] == "OPERATOR"
            ref = json.dumps(operation["requestBody"])
            assert "provider_event.json" in ref, "回调必须使用 provider_event Schema"
        if path == "/v1/meta/tools":
            assert operation["x-agent-exposed"] is True, "工具目录必须对模型可见"


# --------------------------------------------------------------------------
# 7 模块注册表
# --------------------------------------------------------------------------
def test_module_manifest_is_consistent():
    manifest = yaml.safe_load((CONTRACTS / "module-manifest.yaml").read_text(encoding="utf-8"))
    modules = manifest["modules"]
    ids = [m["id"] for m in modules]
    assert len(ids) == len(set(ids)), "模块 id 必须唯一"
    known = set(ids)
    for module in modules:
        assert module["owner"] in {"A", "B", "C", "D"}, f"{module['id']} 的 owner 非法"
        for co in module.get("co_owners", []):
            assert co in {"A", "B", "C", "D"}
        assert module["status"] in {"planned", "in_progress", "integrated"}, f"{module['id']} 状态非法"
        assert module["provides"], f"{module['id']} 必须声明 provides"
        for dep in module["depends_on"]:
            assert dep in known, f"{module['id']} 依赖未登记模块 {dep}"
        assert module["path"], f"{module['id']} 必须声明 path"
        if module["status"] == "in_progress":
            assert (REPO_ROOT / module["path"]).exists(), f"{module['id']} 标记 in_progress，但 {module['path']} 不存在"
        target = REPO_ROOT / module["path"]
        assert target.is_absolute() or not str(target).startswith("..")


def test_module_manifest_covers_all_work_packages():
    manifest = yaml.safe_load((CONTRACTS / "module-manifest.yaml").read_text(encoding="utf-8"))
    covered = {wp for module in manifest["modules"] for wp in module.get("work_packages", [])}
    expected = {f"{owner}{index:02d}" for owner in "ABCD" for index in range(1, 7)}
    assert covered == expected, f"24 个工作包未全部登记：缺少={sorted(expected - covered)}"


def test_module_manifest_links_contract_consumers():
    manifest = yaml.safe_load((CONTRACTS / "module-manifest.yaml").read_text(encoding="utf-8"))
    modules = {m["id"]: m for m in manifest["modules"]}
    assert "contracts" in modules
    assert modules["contracts"]["owner"] == "D", "契约包由 D 汇总维护（GOV-01）"
    dependents = {m["id"] for m in manifest["modules"] if "contracts" in m["depends_on"]}
    assert {"intent-compiler", "orchestration-planner", "security-policy", "tests-contracts"} <= dependents