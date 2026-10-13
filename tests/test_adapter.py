"""Tests for the NapCat platform adapter (OneBot 11 reverse WebSocket).

Ported from NousResearch/hermes-agent PR #17917 ``tests/gateway/test_napcat.py``
and extended with coverage for the review fixes this refactor carries: the
group/DM allowlists, message de-duplication, bounded WS frame size, and
Markdown-stripping on outbound text.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

from gateway.config import PlatformConfig

from plugin.adapter import (
    DEFAULT_TYPING_TTL_SECONDS,
    TYPING_KEEPALIVE_INTERVAL_SECONDS,
    WS_MAX_MSG_SIZE,
    NapCatAdapter,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_config(**extra) -> PlatformConfig:
    return PlatformConfig(enabled=True, extra=extra)


def _make_adapter(**extra):
    """Construct a NapCatAdapter with minimal valid config."""
    extra.setdefault("token", "test-token")
    return NapCatAdapter(_make_config(**extra))


def _run(coro):
    return asyncio.run(coro)


def _ok(data=None, message_id="m1"):
    """Build an OneBot ok response with optional message_id payload."""
    payload = {"status": "ok", "retcode": 0, "data": data or {}}
    if message_id is not None and "message_id" not in payload["data"]:
        payload["data"]["message_id"] = message_id
    return payload


# ---------------------------------------------------------------------------
# call_action / _call_action alias
# ---------------------------------------------------------------------------


class TestCallActionAlias:
    def test_public_and_private_are_same_callable(self):
        # Both names must resolve to the same coroutine function so the
        # internal _send_chunk path keeps working alongside napcat_call's
        # use of the public `call_action`.
        assert NapCatAdapter.call_action is NapCatAdapter._call_action

    def test_direct_upload_private_file_stream_uploads_local_file_first(self, tmp_path):
        adapter = _make_adapter()
        adapter._mark_connected()
        adapter._stream_upload_chunk_size = 4
        local_file = tmp_path / "hermes_napcat_file_test.txt"
        file_bytes = b"hello from hermes"
        local_file.write_bytes(file_bytes)
        uploaded_path = r"C:\NapCat\Temp\hermes_napcat_file_test.txt"
        payloads = []

        class FakeWebSocket:
            closed = False

            async def send_json(self, payload):
                payloads.append(payload)
                action = payload["action"]
                params = payload["params"]
                if action == "upload_file_stream" and params.get("is_complete"):
                    response = {
                        "status": "ok",
                        "retcode": 0,
                        "data": {"status": "file_complete", "file_path": uploaded_path},
                        "echo": payload["echo"],
                    }
                elif action == "upload_file_stream":
                    response = {
                        "status": "ok",
                        "retcode": 0,
                        "data": {"received_chunks": params["chunk_index"] + 1},
                        "echo": payload["echo"],
                    }
                else:
                    response = _ok(message_id="uploaded")
                    response["echo"] = payload["echo"]
                adapter._pending_responses[payload["echo"]][1].set_result(response)

        adapter._ws = FakeWebSocket()

        result = _run(
            adapter.call_action(
                "upload_private_file",
                {"user_id": 10001, "file": str(local_file), "name": local_file.name},
            )
        )

        assert result["status"] == "ok"
        actions = [payload["action"] for payload in payloads]
        assert "upload_file_stream" in actions
        assert actions[-1] == "upload_private_file"
        assert payloads[-1]["params"]["file"] == uploaded_path

    def test_direct_upload_private_file_combines_stream_and_upload_errors(self, tmp_path):
        adapter = _make_adapter()
        adapter._mark_connected()
        local_file = tmp_path / "hermes_napcat_file_test.txt"
        local_file.write_text("hello")

        class FakeWebSocket:
            closed = False

            async def send_json(self, payload):
                if payload["action"] == "upload_file_stream":
                    response = {
                        "status": "failed",
                        "retcode": 404,
                        "message": "STREAM_UNSUPPORTED",
                        "echo": payload["echo"],
                    }
                else:
                    response = {
                        "status": "failed",
                        "retcode": 1,
                        "message": "识别URL失败",
                        "echo": payload["echo"],
                    }
                adapter._pending_responses[payload["echo"]][1].set_result(response)

        adapter._ws = FakeWebSocket()

        result = _run(
            adapter.call_action(
                "upload_private_file",
                {"user_id": 10001, "file": str(local_file), "name": local_file.name},
            )
        )

        assert result["status"] == "failed"
        assert "STREAM_UNSUPPORTED" in result["message"]
        assert "识别URL失败" in result["message"]


# ---------------------------------------------------------------------------
# WS frame size — reviewer fix #1 (bounded max_msg_size)
# ---------------------------------------------------------------------------


class TestBoundedFrameSize:
    def test_frame_cap_is_bounded(self):
        assert WS_MAX_MSG_SIZE == 4 * 1024 * 1024


# ---------------------------------------------------------------------------
# Allowlists — reviewer fix #2 (NAPCAT_ALLOWED_GROUPS & friends are enforced)
# ---------------------------------------------------------------------------


class TestAllowlistGating:
    def _private_payload(self, sender=10001, message_id=1):
        return {
            "post_type": "message",
            "message_type": "private",
            "message_id": message_id,
            "user_id": sender,
            "sender": {"user_id": sender, "nickname": "u"},
            "message": [{"type": "text", "data": {"text": "hi"}}],
        }

    def _group_payload(self, sender=20002, group=30003, message_id=1, self_id="1000"):
        return {
            "post_type": "message",
            "message_type": "group",
            "message_id": message_id,
            "group_id": group,
            "user_id": sender,
            "sender": {"user_id": sender, "card": "g"},
            "message": [
                {"type": "at", "data": {"qq": self_id}},
                {"type": "text", "data": {"text": "hello bot"}},
            ],
        }

    def test_dm_open_by_default(self):
        adapter = _make_adapter()
        adapter._self_id = "1000"
        assert adapter._build_message_event(self._private_payload(sender=99999)) is not None

    def test_dm_allowlist_denies_stranger(self):
        adapter = _make_adapter(allow_from=["10001"])
        adapter._self_id = "1000"
        assert adapter._build_message_event(self._private_payload(sender=99999)) is None
        assert adapter._build_message_event(self._private_payload(sender=10001, message_id=2)) is not None

    def test_group_open_by_default(self):
        adapter = _make_adapter()
        adapter._self_id = "1000"
        assert adapter._build_message_event(self._group_payload()) is not None

    def test_group_allowlist_denies_other_groups(self):
        adapter = _make_adapter(allowed_groups=["30003"])
        adapter._self_id = "1000"
        assert adapter._build_message_event(self._group_payload(group=88888, message_id=11)) is None
        assert adapter._build_message_event(self._group_payload(group=30003, message_id=12)) is not None

    def test_group_allowlist_wildcard_admits_any_group(self):
        adapter = _make_adapter(allowed_groups=["*"])
        adapter._self_id = "1000"
        assert adapter._build_message_event(self._group_payload(group=88888, message_id=13)) is not None

    def test_group_member_allowlist_denies_stranger(self):
        adapter = _make_adapter(group_allow_from=["20002"])
        adapter._self_id = "1000"
        assert adapter._build_message_event(self._group_payload(sender=70007, message_id=14)) is None
        assert adapter._build_message_event(self._group_payload(sender=20002, message_id=15)) is not None

    def test_member_allowlist_still_applies_inside_allowed_group(self):
        adapter = _make_adapter(allowed_groups=["30003"], group_allow_from=["20002"])
        adapter._self_id = "1000"
        # Right group, wrong member.
        assert adapter._build_message_event(self._group_payload(group=30003, sender=70007, message_id=16)) is None

    def test_allow_all_skips_every_list(self):
        adapter = _make_adapter(
            allow_from=["10001"], allowed_groups=["30003"], group_allow_from=["20002"],
            allow_all=True,
        )
        adapter._self_id = "1000"
        assert adapter._build_message_event(self._private_payload(sender=55555, message_id=17)) is not None
        assert adapter._build_message_event(self._group_payload(group=88888, sender=77777, message_id=18)) is not None


# ---------------------------------------------------------------------------
# De-duplication
# ---------------------------------------------------------------------------


class TestDedup:
    def test_same_message_id_only_once(self):
        adapter = _make_adapter()
        adapter._self_id = "1000"
        payload = {
            "post_type": "message",
            "message_type": "private",
            "message_id": 42,
            "user_id": 10001,
            "sender": {"user_id": 10001},
            "message": [{"type": "text", "data": {"text": "hi"}}],
        }
        assert adapter._build_message_event(payload) is not None
        assert adapter._build_message_event(payload) is None  # duplicate dropped


# ---------------------------------------------------------------------------
# Handshake auth
# ---------------------------------------------------------------------------


class _FakeQuery(dict):
    def get(self, key, default=None):
        return dict.get(self, key, default)


class _FakeRequest:
    def __init__(self, headers=None, query=None):
        self.headers = headers or {}
        self.rel_url = SimpleNamespace(query=_FakeQuery(query or {}))
        self.query = _FakeQuery(query or {})


class TestAuth:
    def test_bearer_header_accepted(self):
        adapter = _make_adapter()
        req = _FakeRequest(headers={"Authorization": "Bearer test-token"})
        assert adapter._is_authorized_request(req) is True

    def test_plain_header_accepted(self):
        adapter = _make_adapter()
        req = _FakeRequest(headers={"Authorization": "test-token"})
        assert adapter._is_authorized_request(req) is True

    def test_query_param_accepted(self):
        adapter = _make_adapter()
        req = _FakeRequest(query={"access_token": "test-token"})
        assert adapter._is_authorized_request(req) is True

    def test_bad_token_rejected(self):
        adapter = _make_adapter()
        req = _FakeRequest(headers={"Authorization": "Bearer wrong"}, query={"access_token": "also-wrong"})
        assert adapter._is_authorized_request(req) is False

    def test_missing_token_rejected(self):
        adapter = _make_adapter()
        assert adapter._is_authorized_request(_FakeRequest()) is False


# ---------------------------------------------------------------------------
# _segment_marker — inbound rich-media markers
# ---------------------------------------------------------------------------


class TestSegmentMarker:
    def test_image_with_file_id(self):
        marker = NapCatAdapter._segment_marker("image", {"file": "abcd1234"})
        assert marker == "[图片:abcd1234]"

    def test_image_falls_back_to_url(self):
        marker = NapCatAdapter._segment_marker("image", {"url": "https://x.example/y.png"})
        assert marker == "[图片:https://x.example/y.png]"

    def test_record_segment(self):
        marker = NapCatAdapter._segment_marker("record", {"file": "voice123"})
        assert marker == "[语音:voice123]"

    def test_file_segment_with_name_and_id(self):
        marker = NapCatAdapter._segment_marker(
            "file", {"name": "report.pdf", "file_id": "/biz/abc"}
        )
        assert marker == "[文件:report.pdf:/biz/abc]"

    def test_face_segment(self):
        marker = NapCatAdapter._segment_marker("face", {"id": "76"})
        assert marker == "[表情:76]"

    def test_unknown_segment_returns_none(self):
        assert NapCatAdapter._segment_marker("xml", {"data": "..."}) is None


# ---------------------------------------------------------------------------
# _extract_reply_and_text — private chats & at segments
# ---------------------------------------------------------------------------


class TestExtractReplyAndText:
    def test_text_only(self):
        reply, text = NapCatAdapter._extract_reply_and_text(
            [{"type": "text", "data": {"text": "hello"}}]
        )
        assert reply is None
        assert text == "hello"

    def test_reply_segment_extracted(self):
        reply, text = NapCatAdapter._extract_reply_and_text(
            [
                {"type": "reply", "data": {"id": "999"}},
                {"type": "text", "data": {"text": "hi"}},
            ]
        )
        assert reply == "999"
        assert text == "hi"

    def test_image_segment_becomes_marker(self):
        _, text = NapCatAdapter._extract_reply_and_text(
            [
                {"type": "text", "data": {"text": "看这个"}},
                {"type": "image", "data": {"file": "fid_xyz"}},
            ]
        )
        assert "看这个" in text
        assert "[图片:fid_xyz]" in text

    def test_at_segment_includes_qq_marker(self):
        _, text = NapCatAdapter._extract_reply_and_text(
            [
                {"type": "at", "data": {"qq": "12345"}},
                {"type": "text", "data": {"text": "ping"}},
            ]
        )
        # In the private/text-extraction path (no self_id stripping), an at
        # segment is surfaced as @<qq> for the agent.
        assert "@12345" in text
        assert "ping" in text

    def test_cq_string_format_stripped_to_text(self):
        segments = NapCatAdapter._normalize_segments("看这个[CQ:image,file=abc.jpg]")
        assert segments == [{"type": "text", "data": {"text": "看这个"}}]

    def test_dict_message_wrapped_as_single_segment(self):
        segments = NapCatAdapter._normalize_segments({"type": "text", "data": {"text": "x"}})
        assert segments == [{"type": "text", "data": {"text": "x"}}]

    def test_build_message_event_populates_reply_to_text_from_get_msg(self):
        adapter = _make_adapter()
        adapter._mark_connected()
        adapter._self_id = "3574568714"

        async def fake_call_action(action, params, *, timeout=None):
            assert action == "get_msg"
            assert params == {"message_id": 12345}
            return {
                "status": "ok",
                "retcode": 0,
                "data": {
                    "message_id": 12345,
                    "sender": {"card": "迷茫喵", "nickname": "cat"},
                    "message": [
                        {"type": "text", "data": {"text": "看这张"}},
                        {"type": "image", "data": {"file": "quoted_img_abc"}},
                    ],
                },
            }

        adapter.call_action = fake_call_action
        event = _run(
            adapter._build_message_event_with_reply_context(
                {
                    "post_type": "message",
                    "message_type": "group",
                    "message_id": 67890,
                    "group_id": 977185513,
                    "user_id": 508075486,
                    "sender": {"card": "霖"},
                    "message": [
                        {"type": "reply", "data": {"id": "12345"}},
                        {"type": "at", "data": {"qq": "3574568714"}},
                        {"type": "text", "data": {"text": "识别一下这张图片"}},
                    ],
                }
            )
        )

        assert event is not None
        assert event.text == "识别一下这张图片"
        assert event.reply_to_message_id == "12345"
        assert event.reply_to_text == "[迷茫喵] 看这张 [图片:quoted_img_abc]"


# ---------------------------------------------------------------------------
# _strip_self_mention — group chat gating
# ---------------------------------------------------------------------------


class TestStripSelfMention:
    def test_no_self_id_returns_not_mentioned(self):
        adapter = _make_adapter()
        adapter._self_id = None
        mentioned, text = adapter._strip_self_mention(
            [{"type": "text", "data": {"text": "hi"}}]
        )
        assert mentioned is False
        assert text == ""

    def test_at_self_marks_mentioned_and_strips(self):
        adapter = _make_adapter()
        adapter._self_id = "1000"
        mentioned, text = adapter._strip_self_mention(
            [
                {"type": "at", "data": {"qq": "1000"}},
                {"type": "text", "data": {"text": " 帮我看看"}},
            ]
        )
        assert mentioned is True
        assert text == "帮我看看"

    def test_at_other_kept_as_marker_no_mention(self):
        adapter = _make_adapter()
        adapter._self_id = "1000"
        mentioned, text = adapter._strip_self_mention(
            [
                {"type": "at", "data": {"qq": "2000"}},
                {"type": "text", "data": {"text": "看这"}},
            ]
        )
        assert mentioned is False
        assert "@2000" in text
        assert "看这" in text

    def test_image_and_file_become_markers_when_mentioned(self):
        adapter = _make_adapter()
        adapter._self_id = "1000"
        segments = [
            {"type": "at", "data": {"qq": "1000"}},
            {"type": "text", "data": {"text": "这是啥"}},
            {"type": "image", "data": {"file": "img_1"}},
            {"type": "file", "data": {"name": "a.txt", "file_id": "fid_a"}},
        ]
        mentioned, text = adapter._strip_self_mention(segments)
        assert mentioned is True
        assert "[图片:img_1]" in text
        assert "[文件:a.txt:fid_a]" in text
        assert "这是啥" in text


# ---------------------------------------------------------------------------
# meta_event self_id tracking
# ---------------------------------------------------------------------------


class TestMetaEventSelfId:
    def test_meta_event_updates_self_id(self):
        adapter = _make_adapter()
        adapter._handle_meta_event({
            "post_type": "meta_event",
            "meta_event_type": "lifecycle",
            "sub_type": "connect",
            "self_id": 2795487017,
        })
        assert adapter._self_id == "2795487017"


# ---------------------------------------------------------------------------
# _resolve_chat_target
# ---------------------------------------------------------------------------


class TestResolveChatTarget:
    def test_cached_chat_type_wins(self):
        adapter = _make_adapter()
        adapter._chat_type_map["12345"] = "group"
        chat_type, normalized = adapter._resolve_chat_target("12345")
        assert chat_type == "group"
        assert normalized == "12345"

    def test_group_prefix_explicit(self):
        adapter = _make_adapter()
        chat_type, normalized = adapter._resolve_chat_target("group:99999")
        assert chat_type == "group"
        assert normalized == "99999"

    def test_private_prefix_explicit(self):
        adapter = _make_adapter()
        chat_type, normalized = adapter._resolve_chat_target("private:10001")
        assert chat_type == "private"
        assert normalized == "10001"

    def test_bare_numeric_falls_back_to_private(self):
        adapter = _make_adapter()
        chat_type, normalized = adapter._resolve_chat_target("42")
        assert chat_type == "private"
        assert normalized == "42"


# ---------------------------------------------------------------------------
# _media_file_uri — local path / URL handling
# ---------------------------------------------------------------------------


class TestMediaFileUri:
    def test_http_url_unchanged(self):
        assert NapCatAdapter._media_file_uri("https://x.example/img.png") == \
            "https://x.example/img.png"

    def test_file_uri_unchanged(self):
        assert NapCatAdapter._media_file_uri("file:///tmp/x.png") == "file:///tmp/x.png"

    def test_base64_unchanged(self):
        assert NapCatAdapter._media_file_uri("base64://aGVsbG8=") == "base64://aGVsbG8="

    def test_unix_absolute_path_becomes_file_uri(self):
        result = NapCatAdapter._media_file_uri("/tmp/img.png")
        assert result.startswith("file://")
        assert result.endswith("/tmp/img.png")

    def test_empty_returns_empty(self):
        assert NapCatAdapter._media_file_uri("") == ""


# ---------------------------------------------------------------------------
# Outbound text — markdown stripping, chunking, timeouts
# ---------------------------------------------------------------------------


class TestSendText:
    def _connected(self, chat_id="10001", chat_type="private"):
        adapter = _make_adapter()
        adapter._mark_connected()
        ws = MagicMock()
        ws.closed = False
        adapter._ws = ws
        adapter._chat_type_map[chat_id] = chat_type
        return adapter

    def test_markdown_is_stripped(self):
        adapter = self._connected()
        captured = {}

        async def fake(action, params, *, timeout=None):
            captured["action"] = action
            captured["params"] = params
            return _ok()

        adapter.call_action = fake
        adapter._call_action = fake
        result = _run(adapter.send("10001", "## 标题\n\n**加粗** `code`"))
        assert result.success is True
        text_seg = captured["params"]["message"][0]
        assert "**" not in text_seg["data"]["text"]
        assert "加粗" in text_seg["data"]["text"]
        assert "【标题】" in text_seg["data"]["text"]

    def test_reply_segment_only_on_first_chunk(self):
        adapter = self._connected()
        calls = []

        async def fake(action, params, *, timeout=None):
            calls.append(params)
            return _ok()

        adapter.call_action = fake
        adapter._call_action = fake
        long_text = "\n".join(f"line {i}" for i in range(600))
        result = _run(adapter.send("10001", long_text, reply_to="4242"))
        assert result.success is True
        assert len(calls) > 1
        first, rest = calls[0], calls[1:]
        assert first["message"][0]["type"] == "reply"
        for params in rest:
            assert all(seg["type"] != "reply" for seg in params["message"])

    def test_send_timeout_is_not_retryable(self):
        adapter = self._connected()

        async def fake(action, params, *, timeout=None):
            raise asyncio.TimeoutError()

        adapter.call_action = fake
        adapter._call_action = fake
        result = _run(adapter.send("10001", "hello"))
        assert result.success is False
        assert "timed out" in result.error
        assert not result.retryable  # may already have been delivered

    def test_not_connected_is_retryable(self):
        adapter = self._connected()
        adapter._ws = None
        result = _run(adapter.send("10001", "hello"))
        assert result.success is False
        assert result.retryable is True


# ---------------------------------------------------------------------------
# Outbound media — send_image_file / send_voice / send_video / send_document
# ---------------------------------------------------------------------------


class TestSendImageFile:
    def _setup_adapter(self):
        adapter = _make_adapter()
        adapter._mark_connected()
        # Build a fake connected websocket so the media short-circuit doesn't fire.
        ws = MagicMock()
        ws.closed = False
        adapter._ws = ws
        adapter._chat_type_map["555"] = "group"
        return adapter

    def test_sends_image_segment_via_send_group_msg(self):
        adapter = self._setup_adapter()
        captured = {}

        async def fake_call_action(action, params, *, timeout=None):
            captured["action"] = action
            captured["params"] = params
            return _ok(message_id="img_msg_1")

        adapter.call_action = fake_call_action  # public
        adapter._call_action = fake_call_action  # private alias
        result = _run(adapter.send_image_file("555", "/tmp/cat.png", caption="meow"))

        assert result.success is True
        assert result.message_id == "img_msg_1"
        assert captured["action"] == "send_group_msg"
        assert captured["params"]["group_id"] == 555

        segments = captured["params"]["message"]
        types = [seg["type"] for seg in segments]
        assert "image" in types
        # Caption appears as a trailing text segment.
        assert "text" in types
        image_seg = next(s for s in segments if s["type"] == "image")
        assert image_seg["data"]["file"].startswith("file://")

    def test_falls_back_to_text_on_failure(self):
        adapter = self._setup_adapter()
        attempts = []

        async def fake_call_action(action, params, *, timeout=None):
            attempts.append(action)
            if action == "send_group_msg" and any(
                seg["type"] == "image" for seg in params.get("message", [])
            ):
                return {"status": "failed", "retcode": 100, "message": "BAD_FILE"}
            # Plain text fallback succeeds.
            return _ok(message_id="text_msg_1")

        adapter.call_action = fake_call_action
        adapter._call_action = fake_call_action
        result = _run(adapter.send_image_file("555", "/tmp/cat.png", caption="cap"))

        # Two send_group_msg calls: image attempt, then text fallback.
        assert attempts == ["send_group_msg", "send_group_msg"]
        # Fallback delivered: report success (original error kept for logs) so
        # the gateway doesn't fire a second failure notification.
        assert result.success is True
        assert result.message_id == "text_msg_1"
        assert result.error == "BAD_FILE"

    def test_existing_local_image_is_stream_uploaded_before_send(self, tmp_path):
        adapter = self._setup_adapter()
        adapter._stream_upload_chunk_size = 4
        image_path = tmp_path / "cat.png"
        image_bytes = b"napcat-image-bytes"
        image_path.write_bytes(image_bytes)
        uploaded_path = r"C:\NapCat\Temp\stream-cat.png"
        calls = []

        async def fake_call_action(action, params, *, timeout=None):
            calls.append((action, params))
            if action == "upload_file_stream" and params.get("is_complete"):
                return {
                    "status": "ok",
                    "retcode": 0,
                    "data": {
                        "status": "file_complete",
                        "file_path": uploaded_path,
                        "file_size": len(image_bytes),
                        "sha256": hashlib.sha256(image_bytes).hexdigest(),
                    },
                }
            if action == "upload_file_stream":
                return {
                    "status": "ok",
                    "retcode": 0,
                    "data": {"received_chunks": params["chunk_index"] + 1},
                }
            return _ok(message_id="img_msg_streamed")

        adapter.call_action = fake_call_action
        adapter._call_action = fake_call_action
        adapter._call_action_raw = fake_call_action
        result = _run(adapter.send_image_file("555", str(image_path), caption="meow"))

        assert result.success is True
        upload_calls = [params for action, params in calls if action == "upload_file_stream"]
        assert len(upload_calls) > 1
        first_chunk = upload_calls[0]
        assert first_chunk["stream_id"]
        assert first_chunk["chunk_index"] == 0
        assert first_chunk["total_chunks"] == 5
        assert first_chunk["filename"] == "cat.png"
        assert first_chunk["file_size"] == len(image_bytes)
        assert first_chunk["file_retention"] == 30_000
        assert "chunk_data" in first_chunk
        assert "chunk" not in first_chunk
        assert upload_calls[-2]["expected_sha256"] == hashlib.sha256(image_bytes).hexdigest()
        assert upload_calls[-1] == {"stream_id": first_chunk["stream_id"], "is_complete": True}
        send_params = next(params for action, params in calls if action == "send_group_msg")
        image_seg = next(s for s in send_params["message"] if s["type"] == "image")
        assert image_seg["data"]["file"] == uploaded_path

    def test_remote_image_url_is_not_stream_uploaded(self):
        adapter = self._setup_adapter()
        calls = []

        async def fake_call_action(action, params, *, timeout=None):
            calls.append((action, params))
            return _ok(message_id="img_url")

        adapter.call_action = fake_call_action
        adapter._call_action = fake_call_action
        result = _run(adapter.send_image_file("555", "https://example.test/cat.png"))

        assert result.success is True
        assert [action for action, _ in calls] == ["send_group_msg"]
        image_seg = next(s for s in calls[0][1]["message"] if s["type"] == "image")
        assert image_seg["data"]["file"] == "https://example.test/cat.png"

    def test_stream_upload_failure_is_returned_with_send_failure(self, tmp_path):
        adapter = self._setup_adapter()
        image_path = tmp_path / "cat.png"
        image_path.write_bytes(b"not-on-napcat")

        async def fake_call_action(action, params, *, timeout=None):
            if action == "upload_file_stream":
                return {"status": "failed", "retcode": 404, "message": "STREAM_UNSUPPORTED"}
            if action == "send_group_msg" and any(
                seg["type"] == "image" for seg in params.get("message", [])
            ):
                return {"status": "failed", "retcode": 100, "message": "ENOENT"}
            return _ok(message_id="fallback_text")

        adapter.call_action = fake_call_action
        adapter._call_action = fake_call_action
        adapter._call_action_raw = fake_call_action
        result = _run(adapter.send_image_file("555", str(image_path)))

        # Text fallback delivered: success reported, both failures preserved.
        assert result.success is True
        assert result.message_id == "fallback_text"
        assert "STREAM_UNSUPPORTED" in result.error
        assert "ENOENT" in result.error


class TestSendVoice:
    def test_sends_record_segment(self):
        adapter = _make_adapter()
        adapter._mark_connected()
        ws = MagicMock()
        ws.closed = False
        adapter._ws = ws
        adapter._chat_type_map["10001"] = "private"

        captured = {}

        async def fake_call_action(action, params, *, timeout=None):
            captured["action"] = action
            captured["params"] = params
            return _ok(message_id="voice_1")

        adapter.call_action = fake_call_action
        adapter._call_action = fake_call_action
        result = _run(adapter.send_voice("10001", "/tmp/hi.ogg"))

        assert result.success is True
        assert captured["action"] == "send_private_msg"
        assert captured["params"]["user_id"] == 10001
        seg_types = [s["type"] for s in captured["params"]["message"]]
        assert "record" in seg_types


class TestSendVideo:
    def test_sends_video_segment(self):
        adapter = _make_adapter()
        adapter._mark_connected()
        ws = MagicMock()
        ws.closed = False
        adapter._ws = ws
        adapter._chat_type_map["999"] = "group"

        captured = {}

        async def fake_call_action(action, params, *, timeout=None):
            captured["action"] = action
            captured["params"] = params
            return _ok(message_id="vid_1")

        adapter.call_action = fake_call_action
        adapter._call_action = fake_call_action
        result = _run(adapter.send_video("999", "/tmp/clip.mp4"))

        assert result.success is True
        seg_types = [s["type"] for s in captured["params"]["message"]]
        assert "video" in seg_types


class TestSendDocument:
    def test_existing_local_document_is_stream_uploaded_before_private_upload(self, tmp_path):
        adapter = _make_adapter()
        adapter._mark_connected()
        adapter._stream_upload_chunk_size = 4
        ws = MagicMock()
        ws.closed = False
        adapter._ws = ws
        adapter._chat_type_map["10001"] = "private"
        local_file = tmp_path / "hermes_napcat_test.svg"
        file_bytes = b"<svg>napcat</svg>"
        local_file.write_bytes(file_bytes)
        uploaded_path = r"C:\NapCat\Temp\hermes_napcat_test.svg"
        calls = []

        async def fake_call_action(action, params, *, timeout=None):
            calls.append((action, params))
            if action == "upload_file_stream" and params.get("is_complete"):
                return {
                    "status": "ok",
                    "retcode": 0,
                    "data": {
                        "status": "file_complete",
                        "file_path": uploaded_path,
                        "file_size": len(file_bytes),
                        "sha256": hashlib.sha256(file_bytes).hexdigest(),
                    },
                }
            if action == "upload_file_stream":
                return _ok({"received_chunks": params["chunk_index"] + 1}, message_id=None)
            return _ok(message_id="doc_msg")

        adapter.call_action = fake_call_action
        adapter._call_action = fake_call_action
        adapter._call_action_raw = fake_call_action
        result = _run(adapter.send_document("10001", str(local_file)))

        assert result.success is True
        actions = [action for action, _ in calls]
        assert "upload_file_stream" in actions
        assert actions[-1] == "upload_private_file"
        upload_params = calls[-1][1]
        assert upload_params["user_id"] == 10001
        assert upload_params["file"] == uploaded_path
        assert upload_params["name"] == "hermes_napcat_test.svg"

    def test_document_stream_upload_failure_is_reported_with_file_upload_failure(self, tmp_path):
        adapter = _make_adapter()
        adapter._mark_connected()
        ws = MagicMock()
        ws.closed = False
        adapter._ws = ws
        adapter._chat_type_map["10001"] = "private"
        local_file = tmp_path / "hermes_napcat_test.svg"
        local_file.write_text("<svg/>")

        async def fake_call_action(action, params, *, timeout=None):
            if action == "upload_file_stream":
                return {"status": "failed", "retcode": 404, "message": "STREAM_UNSUPPORTED"}
            if action == "upload_private_file":
                return {"status": "failed", "retcode": 1, "message": "识别URL失败"}
            return _ok(message_id="notice")

        adapter.call_action = fake_call_action
        adapter._call_action = fake_call_action
        adapter._call_action_raw = fake_call_action
        result = _run(adapter.send_document("10001", str(local_file)))

        assert result.success is True  # fallback notice delivered
        assert result.message_id == "notice"
        assert "STREAM_UNSUPPORTED" in result.error
        assert "识别URL失败" in result.error

    def test_uploads_via_upload_group_file(self):
        adapter = _make_adapter()
        adapter._mark_connected()
        ws = MagicMock()
        ws.closed = False
        adapter._ws = ws
        adapter._chat_type_map["12345"] = "group"

        calls = []

        async def fake_call_action(action, params, *, timeout=None):
            calls.append((action, params))
            return _ok(message_id="file_msg_1")

        adapter.call_action = fake_call_action
        adapter._call_action = fake_call_action
        adapter._call_action_raw = fake_call_action
        result = _run(
            adapter.send_document("12345", "/var/log/app.log", file_name="app.log")
        )

        assert result.success is True
        actions = [c[0] for c in calls]
        assert "upload_group_file" in actions
        params = next(p for a, p in calls if a == "upload_group_file")
        assert params["group_id"] == 12345
        assert params["name"] == "app.log"
        # Path must be absolute & expanded.
        assert os.path.isabs(params["file"])

    def test_private_chat_uploads_via_upload_private_file(self):
        adapter = _make_adapter()
        adapter._mark_connected()
        ws = MagicMock()
        ws.closed = False
        adapter._ws = ws
        adapter._chat_type_map["10001"] = "private"

        captured_action = []

        async def fake_call_action(action, params, *, timeout=None):
            captured_action.append(action)
            return _ok(message_id="f1")

        adapter.call_action = fake_call_action
        adapter._call_action = fake_call_action
        adapter._call_action_raw = fake_call_action
        _run(adapter.send_document("10001", "/tmp/x.txt"))

        assert "upload_private_file" in captured_action

    def test_falls_back_to_text_notice_on_failure(self):
        adapter = _make_adapter()
        adapter._mark_connected()
        ws = MagicMock()
        ws.closed = False
        adapter._ws = ws
        adapter._chat_type_map["12345"] = "group"

        async def fake_call_action(action, params, *, timeout=None):
            if action.startswith("upload_"):
                return {"status": "failed", "retcode": 1, "message": "QUOTA"}
            # text notice succeeds.
            return _ok(message_id="notice")

        adapter.call_action = fake_call_action
        adapter._call_action = fake_call_action
        adapter._call_action_raw = fake_call_action
        result = _run(adapter.send_document("12345", "/tmp/big.bin", file_name="big.bin"))

        # The fallback text notice is sent and reported as delivered; the
        # original upload failure stays on the result for logs.
        assert result.success is True
        assert result.message_id == "notice"
        assert result.error == "QUOTA"


# ---------------------------------------------------------------------------
# Outbound media gating — disconnected adapter
# ---------------------------------------------------------------------------


class TestMediaSendsRequireConnection:
    def test_image_returns_not_connected_when_ws_closed(self):
        adapter = _make_adapter()
        adapter._mark_connected()
        ws = MagicMock()
        ws.closed = True
        adapter._ws = ws

        result = _run(adapter.send_image_file("555", "/tmp/x.png"))
        assert result.success is False
        assert result.retryable is True

    def test_document_returns_not_connected_when_ws_missing(self):
        adapter = _make_adapter()
        adapter._mark_connected()
        adapter._ws = None

        result = _run(adapter.send_document("555", "/tmp/x.txt"))
        assert result.success is False


# ---------------------------------------------------------------------------
# Missing token — startup gate
# ---------------------------------------------------------------------------


class TestConnectRequiresToken:
    def test_connect_fails_without_token(self):
        adapter = NapCatAdapter(_make_config(token=""))
        assert _run(adapter.connect()) is False
        assert adapter.fatal_error_code == "napcat_missing_token"


# ---------------------------------------------------------------------------
# Review fixes — send_image override, splits_long_messages, token sources,
# reply_to_mode, superseded-connection pending isolation
# ---------------------------------------------------------------------------


class TestAdapterClassFlags:
    def test_splits_long_messages_declared(self):
        # Without this the gateway pre-truncates cron output at 4000 chars
        # instead of letting send() chunk at MAX_MESSAGE_LENGTH.
        assert NapCatAdapter.splits_long_messages is True


class TestTokenSources:
    def test_token_from_typed_config_key(self):
        # platforms.napcat.token is a typed key — it lands on config.token,
        # never in extra; the adapter must still see it.
        cfg = PlatformConfig(enabled=True, token="yaml-top-level-token")
        adapter = NapCatAdapter(cfg)
        assert adapter._token == "yaml-top-level-token"

    def test_extra_token_wins_over_typed(self):
        cfg = PlatformConfig(enabled=True, token="typed", extra={"token": "extra"})
        adapter = NapCatAdapter(cfg)
        assert adapter._token == "extra"


class TestSendImageURLOverride:
    def test_url_image_routes_to_native_segment(self):
        adapter = _make_adapter()
        adapter._mark_connected()
        ws = MagicMock()
        ws.closed = False
        adapter._ws = ws
        adapter._chat_type_map["555"] = "group"
        captured = {}

        async def fake_call_action(action, params, *, timeout=None):
            captured["action"] = action
            captured["params"] = params
            return _ok(message_id="img_url_1")

        adapter.call_action = fake_call_action
        adapter._call_action = fake_call_action
        result = _run(adapter.send_image("555", "https://example.test/cat.png", caption="meow"))

        # The base default would send the URL as plain TEXT — the override must
        # deliver a native OneBot image segment instead.
        assert result.success is True
        assert captured["action"] == "send_group_msg"
        segments = captured["params"]["message"]
        image_seg = next(s for s in segments if s["type"] == "image")
        assert image_seg["data"]["file"] == "https://example.test/cat.png"
        assert any(s["type"] == "text" for s in segments)


class TestReplyToMode:
    def _adapter(self, mode):
        from gateway.config import PlatformConfig

        cfg = PlatformConfig(enabled=True, extra={"token": "t"}, reply_to_mode=mode)
        adapter = NapCatAdapter(cfg)
        adapter._mark_connected()
        ws = MagicMock()
        ws.closed = False
        adapter._ws = ws
        adapter._chat_type_map["10001"] = "private"
        return adapter

    @staticmethod
    def _calls(adapter):
        calls = []

        async def fake(action, params, *, timeout=None):
            calls.append(params)
            return _ok()

        adapter.call_action = fake
        adapter._call_action = fake
        return calls

    def test_off_never_threads(self):
        adapter = self._adapter("off")
        calls = self._calls(adapter)
        long_text = "\n".join(f"line {i}" for i in range(600))
        assert _run(adapter.send("10001", long_text, reply_to="42")).success
        assert len(calls) > 1
        assert all(all(seg["type"] != "reply" for seg in p["message"]) for p in calls)

    def test_all_threads_every_chunk(self):
        adapter = self._adapter("all")
        calls = self._calls(adapter)
        long_text = "\n".join(f"line {i}" for i in range(600))
        assert _run(adapter.send("10001", long_text, reply_to="42")).success
        assert len(calls) > 1
        assert all(p["message"][0]["type"] == "reply" for p in calls)


class TestSupersededConnectionPending:
    def test_old_connection_teardown_spares_new_connection_requests(self):
        adapter = _make_adapter()
        old_ws, new_ws = MagicMock(closed=False), MagicMock(closed=False)
        loop = asyncio.new_event_loop()
        try:
            old_fut = loop.create_future()
            new_fut = loop.create_future()
            adapter._pending_responses["e-old"] = (old_ws, old_fut)
            adapter._pending_responses["e-new"] = (new_ws, new_fut)

            # The OLD connection's handler tears down AFTER the new one dialed in.
            adapter._fail_pending_for(old_ws, "Connection closed")

            assert old_fut.done() and old_fut.exception() is not None
            assert not new_fut.done()  # the new connection's call survives
        finally:
            loop.close()


# ---------------------------------------------------------------------------
# Typing indicator — NapCat set_input_status + keep-alive
# ---------------------------------------------------------------------------


class TestTypingIndicator:
    """``set_input_status`` is a NapCat extension with no server-side expiry.

    The adapter arms it, refreshes it while the turn is live, and owns the
    "off" switch — the gateway's single clear races the progress pump.
    """

    def _adapter(self, chat_id="10001", chat_type="private"):
        adapter = _make_adapter()
        adapter._mark_connected()
        ws = MagicMock()
        ws.closed = False
        adapter._ws = ws
        adapter._chat_type_map[chat_id] = chat_type
        return adapter

    @staticmethod
    def _recorder(adapter):
        calls = []

        async def fake(action, params, *, timeout=None):
            calls.append((action, params))
            return _ok()

        adapter.call_action = fake
        adapter._call_action = fake
        return calls

    def test_arms_private_chat_with_string_user_id(self):
        adapter = self._adapter()
        calls = self._recorder(adapter)

        async def scenario():
            await adapter.send_typing("10001")
            assert adapter._typing_task is not None  # keep-alive running
            adapter._cancel_typing_keepalive()

        _run(scenario())
        assert calls == [("set_input_status", {"user_id": "10001", "event_type": 1})]

    def test_group_chat_sends_nothing(self):
        adapter = self._adapter(chat_id="8888", chat_type="group")
        calls = self._recorder(adapter)

        async def scenario():
            await adapter.send_typing("8888")
            adapter._cancel_typing_keepalive()

        _run(scenario())
        assert calls == []  # set_input_status is private-chat only

    def test_stop_typing_clears_and_disarms(self):
        adapter = self._adapter()
        calls = self._recorder(adapter)

        async def scenario():
            await adapter.send_typing("10001")
            await adapter.stop_typing("10001")
            assert adapter._typing_task is None
            assert adapter._typing_chat_id is None

        _run(scenario())
        assert [p["event_type"] for _, p in calls] == [1, 0]

    def test_failure_never_raises(self):
        adapter = self._adapter()

        async def boom(action, params, *, timeout=None):
            raise RuntimeError("websocket gone")

        adapter.call_action = boom
        adapter._call_action = boom

        async def scenario():
            await adapter.send_typing("10001")  # best-effort: must not raise
            adapter._cancel_typing_keepalive()

        _run(scenario())

    def test_disconnect_disarms_the_keepalive(self):
        adapter = self._adapter()

        async def scenario():
            await adapter.send_typing("10001")
            assert adapter._typing_task is not None
            await adapter.disconnect()
            assert adapter._typing_task is None
            assert adapter._typing_chat_id is None

        _run(scenario())
        assert adapter._typing_task is None

    def test_keepalive_refreshes_while_the_turn_is_live(self, monkeypatch):
        from plugin import adapter as adapter_module

        monkeypatch.setattr(adapter_module, "TYPING_KEEPALIVE_INTERVAL_SECONDS", 0.01)
        adapter = self._adapter()
        calls = self._recorder(adapter)
        adapter._typing_chat_id = "10001"
        adapter._typing_deadline = time.monotonic() + 60

        async def scenario():
            task = asyncio.create_task(adapter._typing_keepalive())
            await asyncio.sleep(0.05)
            adapter._typing_chat_id = None  # gateway finished the turn
            await asyncio.wait_for(task, timeout=1)

        _run(scenario())
        assert len(calls) >= 2
        assert {p["event_type"] for _, p in calls} == {1}

    def test_keepalive_self_clears_once_the_gateway_goes_quiet(self, monkeypatch):
        from plugin import adapter as adapter_module

        monkeypatch.setattr(adapter_module, "TYPING_KEEPALIVE_INTERVAL_SECONDS", 0.01)
        adapter = self._adapter()
        calls = self._recorder(adapter)
        adapter._typing_chat_id = "10001"
        adapter._typing_deadline = 0.0  # stale: no refresh for a full TTL

        _run(adapter._typing_keepalive())
        assert calls == [("set_input_status", {"user_id": "10001", "event_type": 0})]
        assert adapter._typing_chat_id is None

    def test_keepalive_exits_when_already_disarmed(self, monkeypatch):
        from plugin import adapter as adapter_module

        monkeypatch.setattr(adapter_module, "TYPING_KEEPALIVE_INTERVAL_SECONDS", 0.01)
        adapter = self._adapter()
        calls = self._recorder(adapter)

        _run(adapter._typing_keepalive())  # never armed
        assert calls == []

    def test_ttl_default_and_config_override(self):
        assert _make_adapter()._typing_ttl == DEFAULT_TYPING_TTL_SECONDS
        assert _make_adapter(typing_ttl_seconds=5)._typing_ttl == 5
        assert TYPING_KEEPALIVE_INTERVAL_SECONDS < DEFAULT_TYPING_TTL_SECONDS


# ---------------------------------------------------------------------------
# edit_message — the gateway's progress pump needs the override
# ---------------------------------------------------------------------------


class TestEditMessage:
    def test_reports_a_non_retryable_failure(self):
        adapter = _make_adapter()
        result = _run(adapter.edit_message("10001", "m1", "hello", finalize=True))
        assert result.success is False
        assert result.retryable is False
        assert "does not support editing" in result.error

    def test_overrides_the_base_noop(self):
        # An inherited base no-op makes the gateway drain and discard the whole
        # tool-progress queue — the adapter must define its own.
        from gateway.platforms.base import BasePlatformAdapter

        assert NapCatAdapter.edit_message is not BasePlatformAdapter.edit_message

    def test_sends_nothing_to_qq(self):
        adapter = _make_adapter()
        calls = []

        async def fake(action, params, *, timeout=None):
            calls.append((action, params))
            return _ok()

        adapter.call_action = fake
        adapter._call_action = fake
        _run(adapter.edit_message("10001", "m1", "hello"))
        assert calls == []
