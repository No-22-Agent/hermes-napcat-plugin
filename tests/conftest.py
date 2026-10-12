"""Test bootstrap: make hermes-agent and the plugin package importable.

The plugin targets the hermes-agent gateway API (``gateway.*``).  Point
``HERMES_AGENT_ROOT`` at a hermes-agent checkout (or pip-install hermes-agent)
and the tests import the plugin straight from this repo's ``plugin/`` package.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def _ensure_hermes_importable() -> None:
    try:
        import gateway.config  # noqa: F401
        return
    except ImportError:
        pass
    candidates = []
    if os.environ.get("HERMES_AGENT_ROOT"):
        candidates.append(os.environ["HERMES_AGENT_ROOT"])
    candidates.extend(["/tmp/hermes-agent", str(Path.home() / "Projects/hermes-agent")])
    for candidate in candidates:
        if candidate and (Path(candidate) / "gateway" / "config.py").exists():
            sys.path.insert(0, candidate)
            return
    raise ImportError(
        "hermes-agent not importable — set HERMES_AGENT_ROOT to a hermes-agent "
        "checkout (needs `pip install aiohttp ruamel.yaml pyyaml` at minimum)"
    )


_ensure_hermes_importable()

# Import the plugin as the `plugin` package from the repo root.
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# `Platform("napcat")` resolves once register() has run; tests construct the
# adapter directly, so pre-create the dynamic member.
from gateway.config import Platform  # noqa: E402

if "napcat" not in Platform._value2member_map_:
    Platform._add_pseudo_member("napcat")
