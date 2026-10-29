from __future__ import annotations

import time
from typing import Optional


class ExecutionRunner:
    def __init__(self, engine, interval_seconds: int = 10):
        self.engine = engine
        self.interval_seconds = max(1, interval_seconds)

    def run(self, max_iterations: Optional[int] = None) -> None:
        count = 0
        while True:
            self.engine.run_once()
            count += 1
            if max_iterations is not None and count >= max_iterations:
                return
            time.sleep(self.interval_seconds)
