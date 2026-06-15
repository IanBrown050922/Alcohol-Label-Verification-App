from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.config import load_config
from src.openai_verifier import smoke_test_model


def main() -> int:
    config = load_config(use_streamlit_secrets=False)
    result = smoke_test_model(config)
    if result.ok:
        print("PASS: " + result.message)
        return 0
    print("FAIL: " + result.message)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
