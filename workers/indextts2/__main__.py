from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="workers.indextts2")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--repo-dir", required=True)
    parser.add_argument("--model-dir", required=True)
    parser.add_argument("--aux-root", required=True)
    parser.add_argument("--jobs-root", required=True)
    parser.add_argument("--engine-lock", required=True)
    parser.add_argument("--checkpoint-lock", required=True)
    parser.add_argument("--environment-lock", required=True)
    parser.add_argument("--environment-freeze", required=True)
    parser.add_argument("--expected-fingerprint-json", required=True)
    parser.add_argument("--device", default="cuda:0")
    parser.add_argument("--fp16", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    repo_dir = Path(args.repo_dir).resolve()
    if not repo_dir.is_dir():
        print(f"repo dir not found: {repo_dir}", file=sys.stderr)
        sys.exit(2)
    project_root = Path(__file__).resolve().parents[2]
    sys.path.insert(0, str(repo_dir))
    sys.path.insert(0, str(project_root))

    # Only now import the worker app / upstream model packages.
    import uvicorn

    from voice_pipeline.models.schemas import EngineFingerprint
    from workers.indextts2.app import create_worker_app
    from workers.indextts2.engine import RealIndexEngine

    expected_fp = EngineFingerprint.model_validate(json.loads(args.expected_fingerprint_json))
    aux_root = Path(args.aux_root)
    aux_paths = {
        "w2v_bert": str(aux_root / "w2v_bert"),
        "semantic_codec": str(aux_root / "maskgct" / "semantic_codec" / "model.safetensors"),
        "campplus": str(aux_root / "campplus" / "campplus_cn_common.bin"),
        "bigvgan": str(aux_root / "bigvgan"),
    }
    engine = RealIndexEngine(Path(args.model_dir), aux_paths, device=args.device)
    worker_app = create_worker_app(
        engine,
        jobs_root=Path(args.jobs_root),
        expected_fingerprint=expected_fp,
    )
    uvicorn.run(worker_app, host=args.host, port=args.port, workers=1, log_level="info")


if __name__ == "__main__":
    main()
