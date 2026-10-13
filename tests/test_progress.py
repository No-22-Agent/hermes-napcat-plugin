"""Tests for plugin-side batched tool progress (``plugin/progress.py`` + adapter).

Covers the policy the plugin owns because QQ cannot edit messages: buffer tool
lines, flush at ``batch_size``, flush a partial batch after ``window_s``, flush
*before* any real outbound message (so tool lines stay above the sentence that
follows them), and never leak a batch into the next turn.
"""

from __future__ import annotations

import asyncio
import re
from types import SimpleNamespace

import pytest

from gateway.config import PlatformConfig

from plugin.adapter import NapCatAdapter
from plugin.formatting import strip_markdown
from plugin.progress import (
    DEFAULT_PROGRESS_BATCH_SIZE,
    DEFAULT_PROGRESS_BATCH_WINDOW_S,
    MAX_LINE_LEN,
    PROGRESS_SEPARATOR,
    ProgressBuffer,
    format_tool_line,
    merge_progress_lines,
    summarize_args,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_adapter(**extra) -> NapCatAdapter:
    extra.setdefault("token", "test-token")
    return NapCatAdapter(PlatformConfig(enabled=True, extra=extra))


class _FakeWS:
    """Minimal WS double: records outbound actions, answers each call ok."""

    closed = False

    def __init__(self, adapter):
        self.adapter = adapter
        self.payloads: list[dict] = []

    async def send_json(self, payload):
        self.payloads.append(payload)
        response = {"status": "ok", "retcode": 0, "data": {"message_id": "m1"},
                    "echo": payload["echo"]}
        self.adapter._pending_responses[payload["echo"]][1].set_result(response)

    async def close(self, **_kwargs):
        self.closed = True

    # -- assertions helpers -------------------------------------------------
    def sent_texts(self) -> list[str]:
        """Text bodies of every outbound message, in send order."""
        out = []
        for payload in self.payloads:
            if payload["action"] not in ("send_private_msg", "send_group_msg"):
                continue
            for segment in payload["params"]["message"]:
                if segment["type"] == "text":
                    out.append(segment["data"]["text"])
        return out

    def actions(self) -> list[str]:
        return [payload["action"] for payload in self.payloads]


def _connected(adapter) -> _FakeWS:
    """Attach a fake WS + loop, as a gateway turn would (connect() + arming)."""
    ws = _FakeWS(adapter)
    adapter._ws = ws
    adapter._mark_connected()
    adapter._loop = asyncio.get_running_loop()
    return ws


async def _flush_pending(ticks: float = 0.02) -> None:
    """Let loop-scheduled callbacks (call_soon_threadsafe / call_later) run."""
    await asyncio.sleep(ticks)


async def _note(adapter: NapCatAdapter, tool_name, args=None) -> None:
    """``note_tool_call`` + yield so the loop-side enqueue has actually run.

    ``note_tool_call`` hands work over with ``call_soon_threadsafe`` (the real
    hook runs on the agent thread), so nothing is buffered until the loop gets a
    turn — assertions must not race that deferral.
    """
    adapter.note_tool_call(tool_name, args)
    await asyncio.sleep(0)


# ---------------------------------------------------------------------------
# Pure rendering
# ---------------------------------------------------------------------------


class TestFormatToolLine:
    def test_known_tool_uses_its_emoji_and_arg_preview(self):
        line = format_tool_line("terminal", {"command": "git status --short"})
        assert line == "🖥 terminal · git status --short"

    def test_unknown_tool_falls_back_to_default_emoji(self):
        line = format_tool_line("mcp__acme__do_thing", {"path": "/tmp/x"})
        assert line.startswith("🔧 mcp__acme__do_thing · /tmp/x")

    def test_no_args_renders_name_only(self):
        assert format_tool_line("memory") == "💾 memory"

    def test_missing_tool_name_still_renders(self):
        assert format_tool_line(None) == "🔧 tool"

    def test_line_stays_single_line(self):
        # A newline inside one entry would break the one-line-per-call reading.
        line = format_tool_line("terminal", {"command": "echo a\necho b"})
        assert "\n" not in line
        assert line == "🖥 terminal · echo a echo b"

    def test_line_is_bounded(self):
        line = format_tool_line("terminal", {"command": "x" * 500})
        assert len(line) <= MAX_LINE_LEN
        assert line.endswith("…")


class TestSummarizeArgs:
    def test_prefers_specific_keys_over_insertion_order(self):
        assert summarize_args({"text": "later", "command": "first"}) == "first"

    def test_skips_container_values(self):
        # dict/list values are noise as a one-line preview; take the scalar one.
        assert summarize_args({"args": {"a": 1}, "path": "/tmp/f"}) == "/tmp/f"

    def test_falls_back_to_first_scalar(self):
        assert summarize_args({"unknown_key": "value"}) == "value"

    def test_empty_inputs(self):
        assert summarize_args(None) == ""
        assert summarize_args({}) == ""
        assert summarize_args({"args": {"a": 1}}) == ""

    def test_truncates_long_values(self):
        preview = summarize_args({"path": "y" * 200})
        assert len(preview) <= 60
        assert preview.endswith("…")


# ---------------------------------------------------------------------------
# Merging a batch into the agent's own message
# ---------------------------------------------------------------------------


class TestMergeProgressLines:
    def test_lines_then_separator_then_text(self):
        assert merge_progress_lines(["a", "b"], "hello") == f"a\nb\n{PROGRESS_SEPARATOR}\nhello"

    def test_no_lines_returns_the_text_unchanged(self):
        assert merge_progress_lines([], "hello") == "hello"

    def test_blank_text_returns_the_lines_alone(self):
        assert merge_progress_lines(["a"], "   ") == "a"
        assert merge_progress_lines(["a"], "") == "a"

    def test_separator_survives_the_markdown_stripper(self):
        # strip_markdown() rewrites ASCII "---" into its own rule, so the merged
        # separator must be glyphs it leaves alone.
        merged = merge_progress_lines(["🖥 terminal · ls"], "回复")
        assert strip_markdown(merged).splitlines()[1] == PROGRESS_SEPARATOR

    def test_order_is_lines_then_text(self):
        merged = merge_progress_lines(["first"], "second")
        assert merged.index("first") < merged.index(PROGRESS_SEPARATOR) < merged.index("second")


# ---------------------------------------------------------------------------
# Buffer state machine
# ---------------------------------------------------------------------------


class TestProgressBuffer:
    def test_add_take_roundtrip(self):
        buf = ProgressBuffer()
        assert buf.empty and len(buf) == 0
        buf.add("10001", "a", now=10.0)
        buf.add("10001", "b", now=12.0)
        assert len(buf) == 2 and buf.chat_id == "10001"
        assert buf.take() == ("10001", ["a", "b"])
        assert buf.empty and buf.chat_id is None and buf.first_ts == 0.0

    def test_age_is_measured_from_the_first_line(self):
        buf = ProgressBuffer()
        buf.add("10001", "a", now=100.0)
        buf.add("10001", "b", now=110.0)
        assert buf.age(115.0) == 15.0

    def test_age_of_empty_buffer_is_zero(self):
        assert ProgressBuffer().age(500.0) == 0.0

    def test_switching_chats_resets_the_buffer(self):
        # One QQ account, one live turn: lines must not leak across turns.
        buf = ProgressBuffer()
        buf.add("10001", "dm-line", now=1.0)
        buf.add("group:222", "group-line", now=2.0)
        assert buf.take() == ("group:222", ["group-line"])

    def test_clear_drops_everything(self):
        buf = ProgressBuffer()
        buf.add("10001", "a", now=1.0)
        buf.clear()
        assert buf.empty and buf.take() == (None, [])


# ---------------------------------------------------------------------------
# Adapter wiring — defaults & config
# ---------------------------------------------------------------------------


class TestBatchingConfig:
    def test_off_by_default(self):
        adapter = _make_adapter()
        assert adapter._progress_batch_enabled is False
        # Guards the numbers the docs promise while the switch is off.
        assert adapter._progress_batch_size == DEFAULT_PROGRESS_BATCH_SIZE == 5
        assert adapter._progress_batch_window_s == DEFAULT_PROGRESS_BATCH_WINDOW_S == 30

    def test_enabled_via_extra(self):
        adapter = _make_adapter(progress_batch=True, progress_batch_size=3,
                                progress_batch_window_s=45)
        assert adapter._progress_batch_enabled is True
        assert adapter._progress_batch_size == 3
        assert adapter._progress_batch_window_s == 45

    def test_enabled_via_env(self, monkeypatch):
        monkeypatch.setenv("NAPCAT_PROGRESS_BATCH", "true")
        monkeypatch.setenv("NAPCAT_PROGRESS_BATCH_SIZE", "7")
        adapter = _make_adapter()
        assert adapter._progress_batch_enabled is True
        assert adapter._progress_batch_size == 7

    @pytest.mark.parametrize("value", ["false", "0", "", "no"])
    def test_falsy_values_keep_it_off(self, value):
        assert _make_adapter(progress_batch=value)._progress_batch_enabled is False

    def test_invalid_numbers_fall_back_to_defaults(self):
        adapter = _make_adapter(progress_batch=True, progress_batch_size="nope",
                                progress_batch_window_s=0)
        assert adapter._progress_batch_size == DEFAULT_PROGRESS_BATCH_SIZE
        assert adapter._progress_batch_window_s == DEFAULT_PROGRESS_BATCH_WINDOW_S


# ---------------------------------------------------------------------------
# Adapter behaviour
# ---------------------------------------------------------------------------


class TestBatchedProgress:
    def test_disabled_adapter_never_buffers(self):
        async def scenario():
            adapter = _make_adapter()  # batching off
            ws = _connected(adapter)
            adapter._typing_chat_id = "10001"
            await _note(adapter, "terminal", {"command": "ls"})
            await _flush_pending()
            assert adapter._progress.empty
            assert ws.sent_texts() == []

        asyncio.run(scenario())

    def test_no_live_turn_means_no_buffer(self):
        async def scenario():
            adapter = _make_adapter(progress_batch=True)
            ws = _connected(adapter)
            # No send_typing() yet → no turn is live in any chat.
            await _note(adapter, "terminal", {"command": "ls"})
            await _flush_pending()
            assert adapter._progress.empty
            assert ws.sent_texts() == []

        asyncio.run(scenario())

    def test_flush_at_batch_size_sends_one_message(self):
        async def scenario():
            adapter = _make_adapter(progress_batch=True, progress_batch_size=3)
            ws = _connected(adapter)
            adapter._typing_chat_id = "10001"

            await _note(adapter, "read_file", {"path": "/a"})
            await _note(adapter, "search_files", {"pattern": "needle"})
            await _flush_pending()
            assert ws.sent_texts() == []  # 2 < 3: still held

            await _note(adapter, "terminal", {"command": "git status"})
            await _flush_pending()
            assert ws.sent_texts() == ["📄 read_file · /a\n🔎 search_files · needle\n🖥 terminal · git status"]

        asyncio.run(scenario())

    def test_partial_batch_flushes_after_the_window(self):
        async def scenario():
            adapter = _make_adapter(progress_batch=True, progress_batch_size=5,
                                    progress_batch_window_s=0.2)
            ws = _connected(adapter)
            adapter._typing_chat_id = "10001"

            await _note(adapter, "terminal", {"command": "sleep 30"})
            await _flush_pending(0.05)
            assert ws.sent_texts() == []  # window not elapsed yet

            await _flush_pending(0.4)
            assert ws.sent_texts() == ["🖥 terminal · sleep 30"]

        asyncio.run(scenario())

    def test_window_is_measured_from_the_first_line_not_the_last(self, monkeypatch):
        # Deterministic: drive the adapter's clock instead of sleeping, and watch
        # the armed deadline — a later line must not push the flush out.
        async def scenario():
            adapter = _make_adapter(progress_batch=True, progress_batch_size=5,
                                    progress_batch_window_s=30)
            _connected(adapter)
            adapter._typing_chat_id = "10001"
            clock = {"t": 1000.0}
            monkeypatch.setattr("plugin.adapter.time.monotonic", lambda: clock["t"])

            adapter._enqueue_progress("read_file", {"path": "/a"})
            first_deadline = adapter._progress_timer.when()
            assert first_deadline - adapter._loop.time() == pytest.approx(30.0, abs=0.5)

            clock["t"] += 20.0  # 20s into the window, a second call arrives
            adapter._enqueue_progress("read_file", {"path": "/b"})
            assert adapter._progress_timer.when() == first_deadline  # deadline unchanged
            assert len(adapter._progress) == 2

        asyncio.run(scenario())

    def test_a_partial_batch_never_waits_for_the_window_if_output_comes(self):
        async def scenario():
            adapter = _make_adapter(progress_batch=True, progress_batch_size=5,
                                    progress_batch_window_s=30)
            ws = _connected(adapter)
            adapter._typing_chat_id = "10001"

            await _note(adapter, "read_file", {"path": "/a"})
            await adapter.send("10001", "先说一句")  # agent speaks → lands now
            assert ws.sent_texts() == [f"📄 read_file · /a\n{PROGRESS_SEPARATOR}\n先说一句"]

        asyncio.run(scenario())

    def test_agent_speech_carries_the_batch_in_one_message(self):
        async def scenario():
            adapter = _make_adapter(progress_batch=True, progress_batch_size=5,
                                    progress_batch_window_s=60)
            ws = _connected(adapter)
            adapter._typing_chat_id = "10001"

            await _note(adapter, "terminal", {"command": "ls"})
            await _note(adapter, "read_file", {"path": "/a"})
            await _flush_pending()
            assert ws.sent_texts() == []  # held: neither size nor window hit

            result = await adapter.send("10001", "这是最终回复")
            assert result.success
            # ONE message, order preserved: tool lines, separator, then the text.
            assert ws.sent_texts() == [
                f"🖥 terminal · ls\n📄 read_file · /a\n{PROGRESS_SEPARATOR}\n这是最终回复"
            ]
            assert adapter._progress.empty

        asyncio.run(scenario())

    def test_message_without_a_batch_is_untouched(self):
        async def scenario():
            adapter = _make_adapter(progress_batch=True)
            ws = _connected(adapter)
            adapter._typing_chat_id = "10001"
            await adapter.send("10001", "没有工具行")
            assert ws.sent_texts() == ["没有工具行"]

        asyncio.run(scenario())

    def test_empty_send_does_not_consume_the_batch(self):
        async def scenario():
            adapter = _make_adapter(progress_batch=True, progress_batch_size=5,
                                    progress_batch_window_s=60)
            _connected(adapter)
            adapter._typing_chat_id = "10001"
            await _note(adapter, "terminal", {"command": "ls"})
            await adapter.send("10001", "   ")  # whitespace-only: a no-op send
            assert len(adapter._progress) == 1  # still pending for the turn end

        asyncio.run(scenario())

    def test_another_chats_batch_is_not_attached(self):
        async def scenario():
            adapter = _make_adapter(progress_batch=True, progress_batch_size=5,
                                    progress_batch_window_s=60)
            ws = _connected(adapter)
            adapter._typing_chat_id = "10001"
            await _note(adapter, "terminal", {"command": "ls"})
            await adapter.send("group:222", "别的会话")
            assert ws.sent_texts() == ["别的会话"]
            assert len(adapter._progress) == 1

        asyncio.run(scenario())

    def test_merged_message_still_chunks_safely(self):
        async def scenario():
            adapter = _make_adapter(progress_batch=True, progress_batch_size=5,
                                    progress_batch_window_s=60)
            ws = _connected(adapter)
            adapter._typing_chat_id = "10001"
            for i in range(3):
                await _note(adapter, "terminal", {"command": f"cmd{i}"})

            # Realistic long body: breakable at spaces (a single unbreakable run
            # longer than MAX_MESSAGE_LENGTH is hard-split by the chunker whether
            # or not progress lines are attached — pre-existing behaviour).
            body = " ".join(["这是一个很长的正文片段"] * 600)  # ~7 KB
            await adapter.send("10001", body)

            texts = ws.sent_texts()
            assert len(texts) > 1                                   # long text split
            assert texts[0].startswith("🖥 terminal · cmd0")         # lines lead
            assert PROGRESS_SEPARATOR in texts[0]                    # divider kept
            assert all(len(text) <= adapter.MAX_MESSAGE_LENGTH for text in texts)

            # Nothing is lost: strip the chunker's " (i/n)" markers and compare.
            cleaned = re.sub(r"\s*\(\d+/\d+\)", "", "".join(texts))
            assert "cmd2" in cleaned
            assert body.replace(" ", "") in cleaned.replace(" ", "")

        asyncio.run(scenario())

    def test_media_send_keeps_the_batch_as_its_own_message(self):
        async def scenario():
            adapter = _make_adapter(progress_batch=True, progress_batch_size=5,
                                    progress_batch_window_s=60)
            ws = _connected(adapter)
            adapter._typing_chat_id = "10001"
            await _note(adapter, "terminal", {"command": "ls"})

            await adapter._send_media_message(
                "10001", "image", "https://example.com/a.png", None, None)
            texts = ws.sent_texts()
            # A caption is not the agent's prose: the tool line stays separate.
            assert texts == ["🖥 terminal · ls"]
            assert "send_private_msg" in ws.actions()

        asyncio.run(scenario())

    def test_stop_typing_flushes_leftovers(self):
        async def scenario():
            adapter = _make_adapter(progress_batch=True, progress_batch_size=5,
                                    progress_batch_window_s=60)
            ws = _connected(adapter)
            adapter._typing_chat_id = "10001"
            await _note(adapter, "web_search", {"query": "hermes"})
            await _flush_pending()

            await adapter.stop_typing("10001")
            assert ws.sent_texts() == ["🌐 web_search · hermes"]
            assert "set_input_status" in ws.actions()

        asyncio.run(scenario())

    def test_disconnect_drops_a_pending_batch(self):
        async def scenario():
            adapter = _make_adapter(progress_batch=True, progress_batch_size=5,
                                    progress_batch_window_s=60)
            ws = _connected(adapter)
            adapter._typing_chat_id = "10001"
            await _note(adapter, "terminal", {"command": "ls"})
            await _flush_pending()

            await adapter.disconnect()
            await _flush_pending()
            assert adapter._progress.empty
            assert "🖥 terminal · ls" not in ws.sent_texts()

        asyncio.run(scenario())

    def test_switching_chat_drops_the_previous_turn_lines(self):
        async def scenario():
            adapter = _make_adapter(progress_batch=True, progress_batch_size=5,
                                    progress_batch_window_s=60)
            ws = _connected(adapter)
            adapter._typing_chat_id = "10001"
            await _note(adapter, "terminal", {"command": "dm"})
            await _flush_pending()

            adapter._typing_chat_id = "group:222"
            await _note(adapter, "terminal", {"command": "group"})
            await _flush_pending()
            await adapter.stop_typing("group:222")
            assert ws.sent_texts() == ["🖥 terminal · group"]

        asyncio.run(scenario())

    def test_flush_before_send_skips_another_chats_batch(self):
        async def scenario():
            adapter = _make_adapter(progress_batch=True, progress_batch_size=5,
                                    progress_batch_window_s=60)
            ws = _connected(adapter)
            adapter._typing_chat_id = "10001"
            await _note(adapter, "terminal", {"command": "ls"})
            await _flush_pending()

            await adapter._flush_progress_before_send("group:222")
            assert ws.sent_texts() == []          # untouched
            assert len(adapter._progress) == 1    # still pending for 10001

        asyncio.run(scenario())

    def test_reentrant_flush_is_a_noop(self):
        async def scenario():
            adapter = _make_adapter(progress_batch=True, progress_batch_size=1)
            ws = _connected(adapter)
            adapter._typing_chat_id = "10001"
            await _note(adapter, "terminal", {"command": "ls"})
            await _flush_pending()

            adapter._progress_flushing = True
            await adapter._flush_progress_batch()
            assert ws.sent_texts() == ["🖥 terminal · ls"]

        asyncio.run(scenario())

    def test_typing_indicator_outlives_the_window_hold(self):
        async def scenario():
            adapter = _make_adapter(progress_batch=True, progress_batch_size=5,
                                    progress_batch_window_s=60)
            ws = _connected(adapter)
            adapter._typing_chat_id = "10001"
            adapter._typing_deadline = 0.0  # as if the gateway's TTL just lapsed
            await _note(adapter, "terminal", {"command": "ls"})
            await _flush_pending()
            # The batch is still held, but the indicator was re-armed for it.
            assert adapter._typing_deadline > 0.0
            assert adapter._typing_task is not None and not adapter._typing_task.done()
            adapter._cancel_typing_keepalive()

        asyncio.run(scenario())

    def test_failed_flush_does_not_resurrect_the_batch(self):
        async def scenario():
            adapter = _make_adapter(progress_batch=True, progress_batch_size=1)
            _connected(adapter)
            adapter._typing_chat_id = "10001"
            adapter._ws = None  # transport gone: the send fails
            await _note(adapter, "terminal", {"command": "ls"})
            await _flush_pending()
            assert adapter._progress.empty

        asyncio.run(scenario())

    def test_note_tool_call_without_a_loop_is_silent(self):
        adapter = _make_adapter(progress_batch=True)
        adapter._typing_chat_id = "10001"
        adapter._loop = None
        adapter.note_tool_call("terminal", {"command": "ls"})  # must not raise
        assert adapter._progress.empty

    def test_note_tool_call_never_raises_on_bad_args(self):
        async def scenario():
            adapter = _make_adapter(progress_batch=True, progress_batch_size=1)
            ws = _connected(adapter)
            adapter._typing_chat_id = "10001"
            await _note(adapter, "terminal", None)          # no args at all
            await _note(adapter, None, {"weird": object()})  # unknown tool + odd args
            await _flush_pending()
            # batch_size=1 → each call flushes on its own; the point is that both
            # odd shapes rendered as plain lines instead of raising.
            assert ws.sent_texts() == ["🖥 terminal", "🔧 tool"]

        asyncio.run(scenario())


class TestRailWarning:
    """The rail-mismatch probe reads config.yaml directly (never gateway.run)."""

    @staticmethod
    def _write_config(tmp_path, monkeypatch, body: str) -> None:
        (tmp_path / "config.yaml").write_text(body, encoding="utf-8")
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    def test_warns_once_when_the_gateway_rail_is_still_on(self, tmp_path, monkeypatch):
        async def scenario():
            adapter = _make_adapter(progress_batch=True, progress_batch_size=5)
            _connected(adapter)
            adapter._typing_chat_id = "10001"
            calls: list[int] = []
            monkeypatch.setattr(adapter, "_warn_if_gateway_progress_rail_on",
                                lambda: calls.append(1))
            await _note(adapter, "terminal", {"command": "ls"})
            await _note(adapter, "terminal", {"command": "ls"})
            await _flush_pending()
            assert calls == [1]  # once per adapter, not once per tool call

        asyncio.run(scenario())

    def test_warns_when_the_rail_is_on(self, tmp_path, monkeypatch, caplog):
        self._write_config(tmp_path, monkeypatch, "display:\n  tool_progress: all\n")
        adapter = _make_adapter(progress_batch=True)
        with caplog.at_level("WARNING"):
            adapter._warn_if_gateway_progress_rail_on()
        assert any("tool_progress" in record.message for record in caplog.records)

    def test_silent_when_the_platform_override_is_off(self, tmp_path, monkeypatch, caplog):
        self._write_config(tmp_path, monkeypatch, (
            "display:\n"
            "  platforms:\n"
            "    napcat:\n"
            "      tool_progress: off\n"
        ))
        adapter = _make_adapter(progress_batch=True)
        with caplog.at_level("WARNING"):
            adapter._warn_if_gateway_progress_rail_on()
        assert not [r for r in caplog.records if "tool_progress" in r.message]

    @pytest.mark.parametrize("body", ["", "display: [not-a-mapping]\n", "not: [valid"])
    def test_unusable_config_never_raises(self, tmp_path, monkeypatch, body):
        # Empty / unparsable / unexpected shape: never raise on the way through.
        self._write_config(tmp_path, monkeypatch, body)
        adapter = _make_adapter(progress_batch=True)
        adapter._warn_if_gateway_progress_rail_on()

    def test_warns_when_no_config_is_readable(self, tmp_path, monkeypatch, caplog):
        # The rail defaults to "all": with no readable config the plugin cannot
        # assume batching replaced anything, so it warns.
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))  # no config.yaml written
        adapter = _make_adapter(progress_batch=True)
        with caplog.at_level("WARNING"):
            adapter._warn_if_gateway_progress_rail_on()
        assert any("tool_progress" in record.message for record in caplog.records)
