"""FinPilot 评测包（V5 19 节、EVAL-01..05、PLAN-08）。

模块边界：

- :mod:`eval.oracle`  —— 基于权威最终状态的判定器（EVAL-03、T49）；
- :mod:`eval.runner`  —— 评测运行器（EVAL-02、EVAL-04、T53）；
- :mod:`eval.baselines` —— 基线与消融的显式配置开关（EVAL-02）；
- :mod:`eval.analysis` —— 统计聚合、配对 bootstrap 与失败归因（EVAL-05）；
- :mod:`eval.formal`  —— 有界状态空间的不变量检查（PLAN-08、T57）。

诚实声明：本包的默认运行模式为 ``mock``（确定性规则编译器 + 确定性本地提供方），
**不是在线大模型结果**；离线模式下的通过率只能读作工程回归信号，
不能读作模型能力结论。详见 ``eval/protocol.md``。
"""

from __future__ import annotations

__all__ = ["ORACLE_VERSION", "RUNNER_VERSION", "PROTOCOL_VERSION"]

ORACLE_VERSION = "oracle-2026.10"
RUNNER_VERSION = "runner-2026.10"
PROTOCOL_VERSION = "eval-protocol-2026.10"