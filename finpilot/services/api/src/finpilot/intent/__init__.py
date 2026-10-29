"""意图层：自然语言 → GoalSpec，以及可回答的澄清。"""

from __future__ import annotations

from .compiler import (  # noqa: F401
    IntentContext,
    blocking_clarifications,
    compile_goal,
    merge_clarification_answer,
)

__all__ = ["IntentContext", "compile_goal", "merge_clarification_answer", "blocking_clarifications"]