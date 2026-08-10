# Security policy

## Supported version

Security fixes target the current `main` branch and the latest tagged release.

## Reporting a vulnerability

Use the repository **Security → Report a vulnerability** private reporting flow. Do not open a
public Issue for path traversal, process control, local file access, secret exposure or dependency
integrity findings.

Include the affected commit, Windows/Python version, minimal reproduction and expected impact.
Remove API keys, private text, voice samples, model weights, database contents and absolute user
paths. A synthetic fake-mode reproduction is preferred.

## Security boundary

The application is a single-user local tool and must remain bound to `127.0.0.1`. It does not
provide authentication, tenant isolation or a hardened public network boundary. Exposing the
control, IndexTTS2 or GPT-SoVITS ports to a LAN or the internet is outside the supported model.

Model downloads are pinned by revision and critical assets are checked against tracked hashes.
Users must review `MODEL_LICENSES.md` and explicitly accept model terms before downloading.

