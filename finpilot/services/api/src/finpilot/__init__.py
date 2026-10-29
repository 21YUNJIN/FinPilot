"""包入口：延迟导入，避免测试或脚本仅需子模块时拉起整个应用。"""

from __future__ import annotations

__version__ = "1.0.0"
__all__ = ["__version__"]