# Third-party notices

This repository contains original orchestration, storage, CLI and WebUI code under the
Apache License 2.0. It depends on separately maintained projects; installing or using those
projects does not change their licenses.

## Inference engines

| Component | Fixed source revision | License |
|---|---|---|
| [IndexTTS2](https://github.com/index-tts/index-tts) | `90ca4d608209584bad3a5bd5becc0b80c146e60f` | [bilibili Model Use License Agreement](https://github.com/index-tts/index-tts/blob/90ca4d608209584bad3a5bd5becc0b80c146e60f/LICENSE) |
| [GPT-SoVITS](https://github.com/RVC-Boss/GPT-SoVITS) | `d523079fc05d9a8028d6085bffe4a2757c32abb6` | [MIT](https://github.com/RVC-Boss/GPT-SoVITS/blob/d523079fc05d9a8028d6085bffe4a2757c32abb6/LICENSE) |

The engine source trees and model weights are downloaded into ignored local directories and
are not copied into this repository or its wheel. See [MODEL_LICENSES.md](MODEL_LICENSES.md).

## Direct Python runtime dependencies

The resolved versions are recorded in `uv.lock`. The following table summarizes the licenses
declared by the direct runtime projects at the time of this release.

| Dependency | Upstream | License |
|---|---|---|
| aiosqlite | https://github.com/omnilib/aiosqlite | MIT |
| Alembic | https://github.com/sqlalchemy/alembic | MIT |
| faster-whisper | https://github.com/SYSTRAN/faster-whisper | MIT |
| FastAPI | https://github.com/fastapi/fastapi | MIT |
| huggingface-hub | https://github.com/huggingface/huggingface_hub | Apache-2.0 |
| HTTPX | https://github.com/encode/httpx | BSD-3-Clause |
| NumPy | https://github.com/numpy/numpy | BSD-3-Clause and bundled component licenses |
| OpenCC | https://github.com/BYVoid/OpenCC | Apache-2.0 |
| portalocker | https://github.com/wolph/portalocker | BSD-3-Clause |
| psutil | https://github.com/giampaolo/psutil | BSD-3-Clause |
| Pydantic | https://github.com/pydantic/pydantic | MIT |
| PyYAML | https://github.com/yaml/pyyaml | MIT |
| RapidFuzz | https://github.com/rapidfuzz/RapidFuzz | MIT |
| silero-vad | https://github.com/snakers4/silero-vad | MIT |
| SoundFile | https://github.com/bastibe/python-soundfile | BSD-3-Clause |
| SQLAlchemy | https://github.com/sqlalchemy/sqlalchemy | MIT |
| Typer | https://github.com/fastapi/typer | MIT |
| Uvicorn | https://github.com/encode/uvicorn | BSD-3-Clause |

Transitive dependencies retain their own notices and license metadata inside their installed
distributions. `config/open-source-reuse.yaml` records the project's reuse decisions and
version-lock references.
