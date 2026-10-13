"""Tests for the napcat_call tool (ported from PR #17917 ``tests/tools/test_napcat_tool.py``,
adapted to the plugin's async handler and ``_live_adapter`` reach-in)."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import MagicMock

import plugin.tools as tools


def _run(coro):
    return asyncio.run(coro)


def _payload(result):
    """Decode a handler result.

    The registry result contract accepts ``str`` only (see
    ``TestRegistryResultContract``), so the handler JSON-encodes every payload.
    """
    assert isinstance(result, str), f"registry wants str results, got {type(result)}"
    return json.loads(result)


class _FakeAdapter:
    def __init__(self, connected=True, response=None, error=None):
        self.is_connected = connected
        self._response = response or {"status": "ok", "retcode": 0, "data": {"message_id": 1}}
        self._error = error
        self.calls = []

    async def call_action(self, action, params, *, timeout=None):
        self.calls.append((action, params))
        if self._error is not None:
            raise self._error
        return self._response


# ---------------------------------------------------------------------------
# Handler validation
# ---------------------------------------------------------------------------


class TestHandlerValidation:
    def test_missing_action(self):
        result = _payload(_run(tools._napcat_call_handler({})))
        assert "error" in result
        assert "action" in result["error"]

    def test_non_string_action(self):
        result = _payload(_run(tools._napcat_call_handler({"action": 123})))
        assert "error" in result

    def test_non_dict_params(self):
        result = _payload(
            _run(tools._napcat_call_handler({"action": "get_status", "params": "oops"}))
        )
        assert "error" in result
        assert "params" in result["error"]

    def test_params_default_empty(self, monkeypatch):
        adapter = _FakeAdapter()
        monkeypatch.setattr(tools, "_get_napcat_adapter", lambda: adapter)
        result = _payload(_run(tools._napcat_call_handler({"action": "get_status"})))
        assert result.get("success") is True
        assert adapter.calls == [("get_status", {})]


# ---------------------------------------------------------------------------
# Registry result contract — str results only
# ---------------------------------------------------------------------------


class TestRegistryResultContract:
    """The registry takes ``str`` tool results (Hermes commit f8361d29c8, #61787).

    A bare ``dict`` return is rejected with ``tool_result_contract`` and the
    model never sees the payload, so every handler path must be JSON-encoded.
    """

    def test_validation_error_is_a_json_string(self):
        raw = _run(tools._napcat_call_handler({}))
        assert isinstance(raw, str)
        assert "action" in json.loads(raw)["error"]

    def test_success_is_a_json_string(self, monkeypatch):
        adapter = _FakeAdapter()
        monkeypatch.setattr(tools, "_get_napcat_adapter", lambda: adapter)
        raw = _run(tools._napcat_call_handler({"action": "get_status"}))
        assert isinstance(raw, str)
        assert json.loads(raw)["success"] is True

    def test_dispatch_failure_is_a_json_string(self, monkeypatch):
        monkeypatch.setattr(tools, "_get_napcat_adapter", lambda: None)
        raw = _run(tools._napcat_call_handler({"action": "get_status"}))
        assert isinstance(raw, str)
        assert "gateway" in json.loads(raw)["error"]

    def test_non_ascii_survives_the_round_trip(self, monkeypatch):
        adapter = _FakeAdapter(response={"status": "ok", "retcode": 0, "data": {"nick": "柯莉耶"}})
        monkeypatch.setattr(tools, "_get_napcat_adapter", lambda: adapter)
        raw = _run(tools._napcat_call_handler({"action": "get_login_info"}))
        assert "柯莉耶" in raw  # ensure_ascii=False keeps QQ nicknames readable
        assert json.loads(raw)["data"]["nick"] == "柯莉耶"


# ---------------------------------------------------------------------------
# Dispatch failures
# ---------------------------------------------------------------------------


class TestDispatchFailures:
    def test_no_adapter_reports_gateway_requirement(self, monkeypatch):
        monkeypatch.setattr(tools, "_get_napcat_adapter", lambda: None)
        result = _run(tools._do_napcat_call("get_status", {}))
        assert "gateway" in result["error"]

    def test_disconnected_adapter_reports_connection(self, monkeypatch):
        adapter = _FakeAdapter(connected=False)
        monkeypatch.setattr(tools, "_get_napcat_adapter", lambda: adapter)
        result = _run(tools._do_napcat_call("get_status", {}))
        assert "not connected" in result["error"]

    def test_timeout_is_reported_not_raised(self, monkeypatch):
        adapter = _FakeAdapter(error=asyncio.TimeoutError())
        monkeypatch.setattr(tools, "_get_napcat_adapter", lambda: adapter)
        result = _run(tools._do_napcat_call("send_group_msg", {}))
        assert "timed out" in result["error"]

    def test_runtime_error_is_reported(self, monkeypatch):
        adapter = _FakeAdapter(error=RuntimeError("NapCat websocket not connected"))
        monkeypatch.setattr(tools, "_get_napcat_adapter", lambda: adapter)
        result = _run(tools._do_napcat_call("get_status", {}))
        assert "failed" in result["error"]

    def test_adapter_without_call_action(self, monkeypatch):
        adapter = MagicMock(spec=["is_connected"])
        adapter.is_connected = True
        monkeypatch.setattr(tools, "_get_napcat_adapter", lambda: adapter)
        result = _run(tools._do_napcat_call("get_status", {}))
        assert "upgrade" in result["error"]


# ---------------------------------------------------------------------------
# Response shaping
# ---------------------------------------------------------------------------


class TestResponseShaping:
    def test_success_shape(self, monkeypatch):
        adapter = _FakeAdapter(response={"status": "ok", "retcode": 0, "data": {"user_id": 1}})
        monkeypatch.setattr(tools, "_get_napcat_adapter", lambda: adapter)
        result = _run(tools._do_napcat_call("get_login_info", {}))
        assert result == {
            "success": True,
            "action": "get_login_info",
            "status": "ok",
            "retcode": 0,
            "data": {"user_id": 1},
        }

    def test_retcode_failure_shape(self, monkeypatch):
        adapter = _FakeAdapter(response={
            "status": "failed", "retcode": 1200, "message": "GROUP_NOT_FOUND",
        })
        monkeypatch.setattr(tools, "_get_napcat_adapter", lambda: adapter)
        result = _run(tools._do_napcat_call("get_group_info", {"group_id": 1}))
        assert "error" in result
        assert result["retcode"] == 1200
        assert "GROUP_NOT_FOUND" in result["error"]
        assert result["raw"]["status"] == "failed"

    def test_params_forwarded_verbatim(self, monkeypatch):
        adapter = _FakeAdapter()
        monkeypatch.setattr(tools, "_get_napcat_adapter", lambda: adapter)
        _run(tools._do_napcat_call("get_group_msg_history", {"group_id": 123, "count": 30}))
        assert adapter.calls == [("get_group_msg_history", {"group_id": 123, "count": 30})]


# ---------------------------------------------------------------------------
# Availability gating
# ---------------------------------------------------------------------------


class TestAvailability:
    def test_available_with_connected_adapter(self, monkeypatch):
        adapter = _FakeAdapter(connected=True)
        monkeypatch.setattr(tools, "_get_napcat_adapter", lambda: adapter)
        assert tools.check_napcat_available() is True

    def test_unavailable_with_disconnected_adapter(self, monkeypatch):
        adapter = _FakeAdapter(connected=False)
        monkeypatch.setattr(tools, "_get_napcat_adapter", lambda: adapter)
        assert tools.check_napcat_available() is False

    def test_unavailable_without_adapter(self, monkeypatch):
        monkeypatch.setattr(tools, "_get_napcat_adapter", lambda: None)
        assert tools.check_napcat_available() is False


# ---------------------------------------------------------------------------
# Registration surface
# ---------------------------------------------------------------------------


class TestRegisterTools:
    def test_schema_and_registration_shape(self):
        assert tools.NAPCAT_CALL_SCHEMA["name"] == "napcat_call"
        assert tools.NAPCAT_CALL_SCHEMA["parameters"]["required"] == ["action"]

        registered = {}

        class FakeCtx:
            def register_tool(self, **kwargs):
                registered.update(kwargs)

        tools.register_tools(FakeCtx())
        assert registered["name"] == "napcat_call"
        assert registered["toolset"] == "napcat"
        assert registered["is_async"] is True
        assert callable(registered["check_fn"])
        assert registered["handler"] is tools._napcat_call_handler
