from __future__ import annotations

import json
import subprocess
from pathlib import Path

MODULE = (
    Path(__file__).parents[2]
    / "src"
    / "voice_pipeline"
    / "webui"
    / "director-lazy-editor.js"
)


def test_sync_lazy_editor_mounts_only_while_open() -> None:
    script = f"""
import {{ syncLazyEditor }} from {json.dumps(MODULE.as_uri())};
let mounted = null;
let mountCalls = 0;
let unmountCalls = 0;
const mount = () => ({{ id: ++mountCalls }});
const unmount = (editor) => {{
  if (editor.id !== 1) throw new Error('unexpected editor');
  unmountCalls += 1;
}};

mounted = syncLazyEditor({{ open: false, mounted, mount, unmount }});
const initiallyClosed = {{ mounted, mountCalls, unmountCalls }};
mounted = syncLazyEditor({{ open: true, mounted, mount, unmount }});
const firstOpen = {{ mounted, mountCalls, unmountCalls }};
mounted = syncLazyEditor({{ open: true, mounted, mount, unmount }});
const repeatedOpen = {{ mounted, mountCalls, unmountCalls }};
mounted = syncLazyEditor({{ open: false, mounted, mount, unmount }});
const closedAgain = {{ mounted, mountCalls, unmountCalls }};

console.log(JSON.stringify({{ initiallyClosed, firstOpen, repeatedOpen, closedAgain }}));
"""

    result = subprocess.run(
        ["node", "--input-type=module", "--eval", script],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )

    assert json.loads(result.stdout) == {
        "initiallyClosed": {"mounted": None, "mountCalls": 0, "unmountCalls": 0},
        "firstOpen": {"mounted": {"id": 1}, "mountCalls": 1, "unmountCalls": 0},
        "repeatedOpen": {"mounted": {"id": 1}, "mountCalls": 1, "unmountCalls": 0},
        "closedAgain": {"mounted": None, "mountCalls": 1, "unmountCalls": 1},
    }
