from __future__ import annotations

from pathlib import Path

from voice_pipeline.runtime.fingerprints import sha256_file


def test_sha256_file_changes_when_weight_changes(tmp_path: Path) -> None:
    weight = tmp_path / "model.pth"
    weight.write_bytes(b"a")
    first = sha256_file(weight)
    weight.write_bytes(b"b")
    assert sha256_file(weight) != first


def test_bundle_sha256_sorts_by_basename_not_path(tmp_path: Path) -> None:
    from voice_pipeline.runtime.fingerprints import bundle_sha256

    a = tmp_path / "a.lock.txt"
    b = tmp_path / "b.lock.txt"
    a.write_text("alpha", encoding="utf-8")
    b.write_text("beta", encoding="utf-8")
    left = bundle_sha256([a, b])
    right = bundle_sha256([b, a])
    assert left == right


def test_bundle_sha256_changes_when_content_changes(tmp_path: Path) -> None:
    from voice_pipeline.runtime.fingerprints import bundle_sha256

    a = tmp_path / "a.lock.txt"
    a.write_text("alpha", encoding="utf-8")
    first = bundle_sha256([a])
    a.write_text("alpha2", encoding="utf-8")
    assert bundle_sha256([a]) != first


def test_compute_engine_fingerprint_reads_lock_revisions(tmp_path: Path) -> None:
    from voice_pipeline.runtime.fingerprints import compute_engine_fingerprint

    engine_lock = tmp_path / "engines.lock.yaml"
    engine_lock.write_text(
        """
schema_version: 1
indextts:
  revision: 90ca4d608209584bad3a5bd5becc0b80c146e60f
  model_revision: 740dcaff396282ffb241903d150ac011cd4b1ede
gpt_sovits:
  revision: d523079fc05d9a8028d6085bffe4a2757c32abb6
  pretrained_revision: 4fae8ec36d3d0373864e580b5d8acfba8da29630
""",
        encoding="utf-8",
    )
    checkpoint_lock = tmp_path / "checkpoints.lock.yaml"
    checkpoint_lock.write_text("schema_version: 1\nassets: []\n", encoding="utf-8")
    env_lock = tmp_path / "env.lock.txt"
    env_lock.write_text("fastapi==0.115.12\n", encoding="utf-8")
    config = tmp_path / "config.yaml"
    config.write_text("device: cuda\n", encoding="utf-8")

    fp = compute_engine_fingerprint(
        "indextts",
        engine_lock_path=engine_lock,
        checkpoint_lock_path=checkpoint_lock,
        env_lock_paths=[env_lock],
        runtime_config_path=config,
    )
    assert fp.source_revision == "90ca4d608209584bad3a5bd5becc0b80c146e60f"
    assert fp.model_revision == "740dcaff396282ffb241903d150ac011cd4b1ede"
    assert len(fp.engine_lock_sha256) == 64
    assert len(fp.environment_lock_sha256) == 64

    fp_gsv = compute_engine_fingerprint(
        "gpt_sovits",
        engine_lock_path=engine_lock,
        checkpoint_lock_path=checkpoint_lock,
        env_lock_paths=[env_lock],
        runtime_config_path=config,
    )
    assert fp_gsv.source_revision == "d523079fc05d9a8028d6085bffe4a2757c32abb6"
    assert fp_gsv.model_revision == "4fae8ec36d3d0373864e580b5d8acfba8da29630"
