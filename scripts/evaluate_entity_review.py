#!/usr/bin/env python
"""命令行评测实体复核精准度。"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

MODULE = ROOT / "test" / "evaluation" / "test_entity_review_quality.py"
spec = importlib.util.spec_from_file_location("entity_review_quality", MODULE)
mod = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(mod)


def main() -> int:
    report = mod.evaluate_fixture()
    print(json.dumps(report, ensure_ascii=False, indent=2))
    gates = report.get("gates") or {}
    return 0 if all(gates.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
