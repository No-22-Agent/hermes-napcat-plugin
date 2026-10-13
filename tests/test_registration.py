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
        self.hooks: dict[str, list] = {}
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

    def register_hook(self, hook_name, callback):
        self.hooks.setdefault(hook_name, []).append(callback)


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

    # Tool-progress batching rides the plugin's pre_tool_call hook.
    assert [cb.__name__ for cb in ctx.hooks.get("pre_tool_call", [])] == ["_on_pre_tool_call"]

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


# ---------------------------------------------------------------------------
# pre_tool_call hook → batched tool progress
# ---------------------------------------------------------------------------


class _RecordingAdapter:
    """Weak-referenceable stand-in for a live adapter."""

    def __init__(self, raises: bool = False):
        self.calls: list[tuple] = []
        self._raises = raises

    def note_tool_call(self, tool_name, args=None):
        if self._raises:
            raise RuntimeError("find me in a debug log, not in the tool call")
        self.calls.append((tool_name, args))


def test_pre_tool_call_hook_reaches_live_adapters():
    plugin._ADAPTERS.clear()
    adapter = _RecordingAdapter()
    plugin._ADAPTERS.add(adapter)
    try:
        plugin._on_pre_tool_call("terminal", {"command": "ls"}, task_id="t1", extra=1)
        assert adapter.calls == [("terminal", {"command": "ls"})]
    finally:
        plugin._ADAPTERS.discard(adapter)


def test_adapter_factory_registers_the_adapter_for_the_hook():
    from gateway.config import PlatformConfig

    plugin._ADAPTERS.clear()
    adapter = plugin._build_adapter(PlatformConfig(enabled=True, extra={"token": "t"}))
    try:
        assert adapter in plugin._ADAPTERS
    finally:
        plugin._ADAPTERS.discard(adapter)


def test_pre_tool_call_hook_swallows_adapter_errors():
    # pre_tool_call is a policy hook: a raising observer must never veto the
    # tool call, so one bad adapter cannot stop the others from being fed.
    plugin._ADAPTERS.clear()
    bad, good = _RecordingAdapter(raises=True), _RecordingAdapter()
    plugin._ADAPTERS.update({bad, good})
    try:
        plugin._on_pre_tool_call("terminal", {})  # must not raise
        assert good.calls == [("terminal", {})]
    finally:
        plugin._ADAPTERS.clear()


def test_register_survives_a_host_without_register_hook():
    # Older/embedded hosts may not expose the hook API: registration must still
    # succeed (batching just stays inactive).
    class _HooklessCtx(_FakeCtx):
        register_hook = None

    ctx = _HooklessCtx()
    plugin.register(ctx)
    assert ctx.platform is not None
