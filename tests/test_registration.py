"""Registration smoke test + standalone sender unit tests."""

from __future__ import annotations

import asyncio

import plugin
from plugin.standalone import _chunk_text, _standalone_send


# ---------------------------------------------------------------------------
# register(ctx) — exercised against the real platform_registry
# ---------------------------------------------------------------------------


class _FakeCtx:
    """Minimal PluginContext double: forwards register_platform to the real
    platform_registry (as PlatformEntry) and records tool / skill registrations."""

    def __init__(self):
        self.tools: list[dict] = []
        self.skills: list[tuple[str, str]] = []
        self.platform: dict | None = None

    def register_platform(self, **kwargs):
        from gateway.platform_registry import PlatformEntry, platform_registry

        field_names = set(PlatformEntry.__dataclass_fields__)
        # The real PluginContext.register_platform forwards unknown keys straight
        # into PlatformEntry (TypeError on typos) — hold ourselves to the same bar.
        explicit = {"name", "label", "adapter_factory", "check_fn", "validate_config",
                    "required_env", "install_hint"}
        unknown = set(kwargs) - field_names - explicit
        assert not unknown, f"register_platform kwargs not in PlatformEntry: {unknown}"
        platform_registry.register(PlatformEntry(
            **{k: v for k, v in kwargs.items() if k in field_names}
        ))
        self.platform = kwargs

    def register_tool(self, **kwargs):
        self.tools.append(kwargs)

    def register_skill(self, name, path, **kwargs):
        self.skills.append((name, str(path)))


def test_register_wires_platform_tool_and_skill():
    ctx = _FakeCtx()
    plugin.register(ctx)

    # Platform registered and Platform("napcat") resolves afterwards.
    from gateway.config import Platform
    from gateway.platform_registry import platform_registry

    assert platform_registry.is_registered("napcat")
    assert Platform("napcat").value == "napcat"

    # The register() kwargs we rely on made it through.
    assert ctx.platform["required_env"] == ["NAPCAT_TOKEN"]
    assert ctx.platform["cron_deliver_env_var"] == "NAPCAT_HOME_CHANNEL"
    assert ctx.platform["allowed_users_env"] == "NAPCAT_ALLOWED_USERS"
    assert callable(ctx.platform["standalone_sender_fn"])
    assert callable(ctx.platform["adapter_factory"])
    assert callable(ctx.platform["check_fn"])
    assert callable(ctx.platform["validate_config"])

    # Tool + skill.
    assert [t["name"] for t in ctx.tools] == ["napcat_call"]
    assert ctx.tools[0]["toolset"] == "napcat"
    assert ctx.skills and ctx.skills[0][0] == "napcat"
    assert ctx.skills[0][1].endswith("SKILL.md")


def test_adapter_factory_builds_adapter():
    ctx = _FakeCtx()
    plugin.register(ctx)
    from gateway.config import PlatformConfig

    adapter = ctx.platform["adapter_factory"](PlatformConfig(enabled=True, extra={"token": "t"}))
    assert adapter.name == "NapCat"


# ---------------------------------------------------------------------------
# validate_config / is_connected probes
# ---------------------------------------------------------------------------


class TestValidateConfig:
    def test_token_in_extra(self):
        from gateway.config import PlatformConfig

        cfg = PlatformConfig(enabled=True, extra={"token": "secret"})
        assert plugin.validate_config(cfg) is True

    def test_token_in_typed_key(self):
        # platforms.napcat.token is a typed config key — validate_config must
        # see it even though it never lands in extra.
        from gateway.config import PlatformConfig

        cfg = PlatformConfig(enabled=True, token="secret")
        assert plugin.validate_config(cfg) is True

    def test_no_token_no_enable(self):
        from gateway.config import PlatformConfig

        cfg = PlatformConfig(enabled=True, extra={})
        assert plugin.validate_config(cfg) is False

    def test_enabled_flag_without_token_does_not_enable(self):
        # Force-enabling without a token would retry napcat_missing_token forever.
        from gateway.config import PlatformConfig

        cfg = PlatformConfig(enabled=True, extra={"enabled": True})
        assert plugin.validate_config(cfg) is False

    def test_token_from_env(self, monkeypatch):
        from gateway.config import PlatformConfig

        monkeypatch.setenv("NAPCAT_TOKEN", "env-secret")
        assert plugin.validate_config(PlatformConfig(enabled=True, extra={})) is True


class TestEnsureDeps:
    def test_passive_when_aiohttp_present(self):
        from plugin.adapter import ensure_napcat_deps

        assert ensure_napcat_deps() is True  # aiohttp importable in the test env

    def test_installs_messaging_extra_when_missing(self, monkeypatch):
        import builtins
        import plugin.adapter as adapter_mod

        calls = []
        real_import = builtins.__import__

        def fake_import(name, *args, **kwargs):
            if name == "pm":
                calls.append(name)

                class _PM:
                    @staticmethod
                    def ensure_import(extra):
                        assert extra == "messaging"

                return _PM
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(adapter_mod, "AIOHTTP_AVAILABLE", False)
        monkeypatch.setattr(builtins, "__import__", fake_import)
        assert adapter_mod.ensure_napcat_deps() is True
        assert calls == ["pm"]


# ---------------------------------------------------------------------------
# Standalone chunking
# ---------------------------------------------------------------------------


class TestChunkText:
    def test_short_text_single_chunk(self):
        assert _chunk_text("hello") == ["hello"]

    def test_long_text_split_at_newline(self):
        text = "\n".join(["x" * 50] * 200)  # > 4500 chars, newline-separated
        chunks = _chunk_text(text)
        assert len(chunks) > 1
        assert all(len(c) <= 4500 for c in chunks)
        assert "".join(c for c in chunks).replace("\n", "") == text.replace("\n", "")

    def test_no_boundary_hard_split(self):
        text = "y" * 9000  # single unbroken run — no newline/space to split on
        chunks = _chunk_text(text)
        assert len(chunks) == 2
        assert all(len(c) <= 4500 for c in chunks)
        assert "".join(chunks) == text


class TestStandaloneSend:
    def test_requires_http_url(self):
        from gateway.config import PlatformConfig

        result = asyncio.run(
            _standalone_send(PlatformConfig(enabled=True, extra={}), "10001", "hi")
        )
        assert "NAPCAT_HTTP_URL" in result["error"]

    def test_invalid_chat_id_rejected(self):
        from gateway.config import PlatformConfig

        cfg = PlatformConfig(enabled=True, extra={"http_url": "http://127.0.0.1:3000", "token": "t"})
        result = asyncio.run(_standalone_send(cfg, "not-a-number", "hi"))
        assert "invalid chat_id" in result["error"]
