from __future__ import annotations

import json
import subprocess
from pathlib import Path

MODULE = Path(__file__).parents[2] / "src" / "voice_pipeline" / "webui" / "service-shutdown.js"


def test_shutdown_coordinator_handles_cancel_accept_disconnect_and_http_error() -> None:
    assert MODULE.is_file(), "service-shutdown.js must provide the shutdown coordinator"
    source = f"""
import {{ confirmAndShutdown }} from {json.dumps(MODULE.as_uri())};

const results = {{}};

let cancelFetches = 0;
results.cancelled = await confirmAndShutdown({{
  confirmShutdown: () => false,
  fetchImpl: async () => {{ cancelFetches += 1; }},
  onStarting: () => {{ throw new Error("cancelled shutdown started"); }},
  onComplete: () => {{ throw new Error("cancelled shutdown completed"); }},
}});
results.cancelFetches = cancelFetches;

const acceptedEvents = [];
results.accepted = await confirmAndShutdown({{
  confirmShutdown: () => true,
  fetchImpl: async (path, options) => {{
    acceptedEvents.push(`${{options.method}} ${{path}}`);
    return {{ ok: true, status: 200 }};
  }},
  onStarting: () => acceptedEvents.push("starting"),
  onComplete: () => acceptedEvents.push("complete"),
}});
results.acceptedEvents = acceptedEvents;

const disconnectedEvents = [];
results.disconnected = await confirmAndShutdown({{
  confirmShutdown: () => true,
  fetchImpl: async () => {{ throw new TypeError("connection closed"); }},
  onStarting: () => disconnectedEvents.push("starting"),
  onComplete: () => disconnectedEvents.push("complete"),
}});
results.disconnectedEvents = disconnectedEvents;

const errorEvents = [];
try {{
  await confirmAndShutdown({{
    confirmShutdown: () => true,
    fetchImpl: async () => ({{
      ok: false,
      status: 500,
      text: async () => "shutdown failed",
    }}),
    onStarting: () => errorEvents.push("starting"),
    onComplete: () => errorEvents.push("complete"),
  }});
}} catch (error) {{
  results.httpError = error.message;
}}
results.errorEvents = errorEvents;

process.stdout.write(JSON.stringify(results));
"""
    completed = subprocess.run(
        ["node", "--input-type=module", "--eval", source],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    result = json.loads(completed.stdout)

    assert result["cancelled"] == {"status": "cancelled"}
    assert result["cancelFetches"] == 0
    assert result["accepted"] == {"status": "accepted"}
    assert result["acceptedEvents"] == [
        "starting",
        "POST /api/v1/control/shutdown",
        "complete",
    ]
    assert result["disconnected"] == {"status": "disconnected"}
    assert result["disconnectedEvents"] == ["starting", "complete"]
    assert "HTTP 500" in result["httpError"]
    assert "shutdown failed" in result["httpError"]
    assert result["errorEvents"] == ["starting"]
