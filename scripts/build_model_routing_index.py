#!/usr/bin/env python3
"""Build the local FAISS index used by the model-routing fallback stage.

Run from the repository root with the backend environment, for example:
``uv run --project backend python scripts/build_model_routing_index.py``.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=None, help="Path to config.yaml; defaults to normal DeerFlow resolution")
    args = parser.parse_args()

    repo_root = Path(__file__).resolve().parents[1]
    backend_dir = repo_root / "backend"
    sys.path.insert(0, str(backend_dir))

    from app.gateway.model_routing.faiss_search import build_faiss_index
    from deerflow.config.app_config import AppConfig

    config = AppConfig.from_file(args.config)
    build_faiss_index(config.model_routing.faiss)
    print(f"Built model-routing FAISS index version {config.model_routing.faiss.index_version}")
    print(f"Wrote model-routing metadata manifest to {config.model_routing.faiss.metadata_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
