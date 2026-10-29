"""容器隔离配置的静态校验（不能替代运行时验证）。

为什么需要它：仓库声明"每个服务只读根文件系统、丢弃全部 capability、非 root、
有资源上限，且数据库与提供方沙箱在无外网出口的内部网络"。这些如果只写在 YAML 注释里，
很容易在后续改动中悄悄丢失。本脚本把声明变成可执行断言，CI 与 pytest 都能跑。

**边界**：静态校验只能证明"配置写了"，不能证明"运行时生效"。真正的隔离验证仍需在
具备容器运行时的 Linux 主机上执行 deploy/README.md 的验证步骤（cgroup 限额、无外网、
只读文件系统、非 root 等），本仓库当前开发机没有 docker/WSL 发行版，未做该运行时验证。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml

APPLIKE = ("migrate", "seed", "api", "worker-outbox", "worker-scheduler", "bank-sandbox", "merchant-sandbox", "web", "im")
REQUIRED_HARDENING = ("read_only", "pids_limit", "mem_limit", "cpus")
FORBIDDEN_VOLUME_MARKERS = ("docker.sock", "/var/run/docker.sock")


def load(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _has(sequence, needle: str) -> bool:
    return isinstance(sequence, list) and any(str(item).startswith(needle) for item in sequence)


def verify(path: Path) -> list[dict]:
    """返回检查项列表；``ok=False`` 的条目即违规。"""
    document = load(path)
    services = document.get("services") or {}
    networks = document.get("networks") or {}
    results: list[dict] = []

    def record(name: str, ok: bool, detail: str) -> None:
        results.append({"check": name, "ok": bool(ok), "detail": detail})

    for name in APPLIKE:
        service = services.get(name)
        if not isinstance(service, dict):
            record(f"{name}.present", False, "服务缺失")
            continue
        record(f"{name}.read_only", service.get("read_only") is True, f"read_only={service.get('read_only')!r}")
        record(f"{name}.cap_drop_all", _has(service.get("cap_drop"), "ALL"), f"cap_drop={service.get('cap_drop')!r}")
        record(
            f"{name}.no_new_privileges",
            _has(service.get("security_opt"), "no-new-privileges"),
            f"security_opt={service.get('security_opt')!r}",
        )
        for key in ("pids_limit", "mem_limit", "cpus"):
            value = service.get(key)
            record(f"{name}.{key}", bool(value), f"{key}={value!r}")
        record(f"{name}.user", bool(service.get("user")), f"user={service.get('user')!r}")

    for name, service in services.items():
        if not isinstance(service, dict):
            continue
        record(f"{name}.not_privileged", service.get("privileged") is not True, f"privileged={service.get('privileged')!r}")
        record(f"{name}.not_host_network", service.get("network_mode") != "host", f"network_mode={service.get('network_mode')!r}")
        volumes = service.get("volumes") or []
        exposed = [volume for volume in volumes if isinstance(volume, str) and any(m in volume for m in FORBIDDEN_VOLUME_MARKERS)]
        record(f"{name}.no_docker_socket", not exposed, f"socket_volumes={exposed}")

    backbone = networks.get("backbone") or {}
    record("networks.backbone_internal", backbone.get("internal") is True, f"internal={backbone.get('internal')!r}")
    record("networks.edge_defined", "edge" in networks, f"networks={sorted(networks)}")

    def nets(name: str) -> list[str]:
        value = (services.get(name) or {}).get("networks") or []
        return value if isinstance(value, list) else [str(value)]

    record("api.on_edge", "edge" in nets("api"), f"api={nets('api')}")
    record("api.on_backbone", "backbone" in nets("api"), f"api={nets('api')}")
    for name in ("postgres", "bank-sandbox", "merchant-sandbox", "worker-outbox", "worker-scheduler"):
        record(f"{name}.backbone_only", nets(name) == ["backbone"], f"{name}={nets(name)}")
    for name in ("web", "im"):
        record(f"{name}.edge_only", nets(name) == ["edge"], f"{name}={nets(name)}")
    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="静态校验 compose 的隔离配置")
    parser.add_argument("--compose", default=str(Path(__file__).with_name("compose.yaml")))
    args = parser.parse_args(argv)

    results = verify(Path(args.compose))
    failed = [item for item in results if not item["ok"]]
    for item in results:
        if not item["ok"]:
            print(f"FAIL  {item['check']}: {item['detail']}")
    print(f"\n共 {len(results)} 项，通过 {len(results) - len(failed)} 项，失败 {len(failed)} 项。")
    if not failed:
        print("静态隔离配置符合声明；运行时隔离仍需在容器主机上另行验证（见 deploy/README.md）。")
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())