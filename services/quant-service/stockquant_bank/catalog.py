"""Load the synthetic bank catalog into request payloads for the service."""
from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any, Dict, List, Optional


def _root(root: Optional[str] = None) -> Path:
    if root:
        return Path(root)
    return Path(__file__).resolve().parents[1]


def load_catalog(root: Optional[str] = None) -> List[Dict[str, Any]]:
    base = _root(root)
    with (base / "data" / "products.json").open(encoding="utf-8") as handle:
        payload = json.load(handle)
    return list(payload["products"])


def load_nav(product: Dict[str, Any], root: Optional[str] = None) -> List[Dict[str, Any]]:
    base = _root(root)
    path = base / product["nav_file"]
    rows: List[Dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            rows.append({"date": row["date"], "value": float(row["nav"])})
    return rows


def request_products(
    product_ids: Optional[List[str]] = None,
    root: Optional[str] = None,
) -> List[Dict[str, Any]]:
    catalog = load_catalog(root)
    wanted = set(product_ids) if product_ids else None
    out: List[Dict[str, Any]] = []
    for product in catalog:
        if wanted and product["product_id"] not in wanted:
            continue
        item = {key: value for key, value in product.items() if key not in {"nav_file"}}
        item["nav"] = load_nav(product, root)
        out.append(item)
    return out