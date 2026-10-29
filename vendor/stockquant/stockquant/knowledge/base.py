from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, Iterable, List


class KnowledgeBase:
    def __init__(self, documents: Iterable[Dict[str, object]]):
        self.documents = list(documents)

    @classmethod
    def load_builtin(cls) -> "KnowledgeBase":
        path = Path(__file__).with_name("core_finance.json")
        return cls(json.loads(path.read_text(encoding="utf-8")))

    def search(self, query: str, limit: int = 5) -> List[Dict[str, object]]:
        terms = [x.strip() for x in query.replace("，", " ").replace(",", " ").split() if x.strip()]
        scored = []
        for doc in self.documents:
            text = " ".join(str(doc.get(k, "")) for k in ("title", "domain", "concept", "evidence", "application", "risk"))
            score = sum(text.lower().count(term.lower()) for term in terms)
            if score:
                scored.append((score, doc))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [doc for _, doc in scored[:limit]]


class LearningEngine:
    """Controlled knowledge distillation.

    This module converts reviewed evidence into explicit hypotheses. It does not
    mutate production weights or execute trades.
    """

    def __init__(self, state_dir: str):
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.kb = KnowledgeBase.load_builtin()

    def weekly_update(self, weekly_review: Dict[str, object]) -> Dict[str, object]:
        worst = weekly_review.get("worst", [])[:5]
        hypotheses = []
        for row in worst:
            query = f"{row.get('symbol')} 预测 错误 风险"
            docs = self.kb.search(query, limit=2)
            for doc in docs:
                hypotheses.append(
                    {
                        "source": doc.get("title"),
                        "trigger": row,
                        "hypothesis": f"检查 {doc.get('concept')} 是否能解释该错误，并在样本外验证。",
                        "validation": doc.get("validation"),
                        "status": "candidate",
                    }
                )
        if not hypotheses:
            for doc in self.kb.documents[:3]:
                hypotheses.append(
                    {
                        "source": doc.get("title"),
                        "trigger": "weekly baseline",
                        "hypothesis": f"保持 {doc.get('concept')} 作为 champion 的约束条件。",
                        "validation": doc.get("validation"),
                        "status": "monitor",
                    }
                )
        payload = {
            "knowledge_documents": len(self.kb.documents),
            "hypotheses": hypotheses,
            "policy": "Candidates require OOS validation and manual promotion; never auto-modify production.",
        }
        (self.state_dir / "knowledge_update.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
        )
        return payload
