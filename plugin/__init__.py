"""NapCat (QQ / OneBot 11) platform plugin for Hermes Agent — registration.

Standard Hermes plugin directory: copy (or symlink) this ``plugin/`` directory
to ``~/.hermes/plugins/napcat/`` — no pip install, no core patches.  Hermes
discovers any subdirectory of ``~/.hermes/plugins/`` with a ``plugin.yaml`` and
an ``__init__.py`` exposing ``register(ctx)``.

Refactor of Zhaosn/hermes-napcat following the implementation approach of
NousResearch/hermes-agent PR #17917: one generic ``napcat_call`` tool plus the
``napcat`` skill instead of 74 ``qq_*`` wrappers, on-demand inbound media
markers instead of eager downloads, ``upload_file_stream`` for cross-machine
file sends, and message de-duplication.

Module layout:
    __init__.py     registration entry (this file) + config probes
    adapter.py      NapCatAdapter — reverse-WS transport / inbound / outbound
    tools.py        napcat_call — generic OneBot 11 action tool
    standalone.py   out-of-process cron delivery via NapCat's HTTP API
    formatting.py   markdown → QQ plain text
    skills/napcat/  operating skill for the agent (output style + catalog)

Platform plugins never read ``os.environ`` for configuration directly — every
env lookup goes through scoped secrets (``gateway.platforms._shared``), and the
plugin never mutates the process environment.
"""

from __future__ import annotations

import logging
from pathlib import Path

from gateway.platforms._shared import (
    get_scoped_secret as _get_scoped_secret,
    seed_extra_from_env as _seed_extra_from_env,
)

from . import tools as _tools
from .adapter import NapCatAdapter, check_napcat_requirements, ensure_napcat_deps
from .formatting import QQ_TEXT_LIMIT
from .standalone import _standalone_send

logger = logging.getLogger(__name__)

__all__ = ["register"]

_TRUTHY = {"1", "true", "yes", "on"}


# ── Passive probes / config helpers (called by the plugin registry) ───────────

def _configured_token(config) -> str:
    """Token across all three spelling places (YAML extra / YAML typed key / scoped env)."""
    extra = getattr(config, "extra", None) or {}
    return str(
        extra.get("token")
        or getattr(config, "token", None)
        or _get_scoped_secret("NAPCAT_TOKEN")
        or ""
    ).strip()


def validate_config(config) -> bool:
    """True when the platform is configured enough to start.

    The token is the one hard requirement (``requires_env`` in plugin.yaml):
    it authenticates NapCat's reverse-WS dial-in and may live in ``.env``
    (``NAPCAT_TOKEN``) or config.yaml (``platforms.napcat.token`` — a typed
    key — or ``platforms.napcat.extra.token``).  Host/port/path all have
    defaults.  Deliberately no "force-enable without token" path: an enabled
    adapter with no token just retries ``napcat_missing_token`` forever
    (config.py #31116 pattern).
    """
    return bool(_configured_token(config))


def is_connected(config) -> bool:
    """Whether the platform is configured enough to auto-enable."""
    return validate_config(config)


def _env_enablement() -> dict | None:
    """Seed ``PlatformConfig.extra`` from the profile's env during config load.

    Pure seeder — no side effects.  Only transport settings are seeded from
    env; behavioral settings (allowlists) are read scoped at adapter
    construction and may also live in config.yaml ``extra``.
    """
    token = str(_get_scoped_secret("NAPCAT_TOKEN") or "").strip()
    enabled = str(_get_scoped_secret("NAPCAT_ENABLED") or "").strip().lower() in _TRUTHY
    if not token and not enabled:
        return None
    seed = _seed_extra_from_env((
        ("NAPCAT_HOST", "host", None),
        ("NAPCAT_PORT", "port", int),
        ("NAPCAT_PATH", "path", None),
        ("NAPCAT_HTTP_URL", "http_url", None),
    ), home_env="NAPCAT_HOME_CHANNEL")
    if token:
        seed["token"] = token
    return seed or None


# ── Plugin entry point ────────────────────────────────────────────────────────

def register(ctx) -> None:
    """Called by the Hermes plugin system during discovery."""
    ctx.register_platform(
        name="napcat",
        label="NapCat (QQ)",
        adapter_factory=lambda cfg: NapCatAdapter(cfg),
        check_fn=check_napcat_requirements,
        ensure_deps_fn=ensure_napcat_deps,
        validate_config=validate_config,
        is_connected=is_connected,
        required_env=["NAPCAT_TOKEN"],
        install_hint=(
            "Place the plugin/ directory into ~/.hermes/plugins/napcat/, make sure "
            "aiohttp is installed, and set NAPCAT_TOKEN to match the token on your "
            "NapCat reverse-WebSocket (websocketClients) item."
        ),
        env_enablement_fn=_env_enablement,
        cron_deliver_env_var="NAPCAT_HOME_CHANNEL",
        standalone_sender_fn=_standalone_send,
        allowed_users_env="NAPCAT_ALLOWED_USERS",
        allow_all_env="NAPCAT_ALLOW_ALL_USERS",
        max_message_length=QQ_TEXT_LIMIT,
        emoji="🐧",
        pii_safe=False,
        platform_hint=(
            "You are chatting via QQ (NapCat / OneBot 11). QQ does not render "
            "Markdown — reply in plain text only, keep it short and "
            "conversational. In groups you are addressed with an explicit @; "
            "inbound images/voice/files appear as [图片:<id>] / [语音:<id>] / "
            "[文件:<name>:<id>] markers — resolve them on demand with the "
            "napcat_call tool (get_image / get_record / get_file)."
        ),
    )

    _tools.register_tools(ctx)

    skill_path = Path(__file__).parent / "skills" / "napcat" / "SKILL.md"
    if skill_path.exists():
        ctx.register_skill("napcat", skill_path)
    else:  # pragma: no cover - packaging regression guard
        logger.warning("NapCat: skill file missing at %s", skill_path)
