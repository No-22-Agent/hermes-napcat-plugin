"""Batched tool-progress lines for QQ (pure logic — no asyncio, no transport).

Why this exists
---------------
QQ/OneBot 11 cannot edit a sent message, so Hermes' per-tool progress rail
(``gateway/run_turn_runner.py::send_progress_messages``) costs **one QQ message
per tool call** on this platform — 10-30 bubbles per turn, each one a phone
notification.  The gateway only coalesces progress on surfaces that support
editing; there is no batching option upstream.

This module holds the plugin's alternative, in the plugin (no core patch):

* the gateway's own rail is switched off (``display.platforms.napcat.tool_progress: off``),
  so every outbound message that reaches the adapter is real agent output;
* the plugin observes tool calls through its ``pre_tool_call`` hook and buffers
  one short line per call;
* the adapter flushes the buffer as a single QQ message when
  - it holds ``batch_size`` lines (default 5), or
  - ``window_s`` seconds passed since its first line (default 30), or
  - anything else is about to be sent (interim commentary / final answer) —
    flushed *before* that message, which is what keeps tool lines ABOVE the
    sentence that follows them instead of scrambling the turn's order.

Deliberately pure: formatting and the buffer state machine are unit-testable
without a WebSocket, a loop, or a connected adapter.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

# Recommended defaults when the feature is switched on (see plugin/plugin.yaml).
DEFAULT_PROGRESS_BATCH_SIZE = 5
DEFAULT_PROGRESS_BATCH_WINDOW_S = 30

# A single progress line is a glanceable status, not a log: keep it short so a
# full batch stays far below QQ's message limit.
MAX_ARG_PREVIEW = 60
MAX_LINE_LEN = 140

DEFAULT_TOOL_EMOJI = "🔧"

# Tool name -> leading emoji.  Missing names degrade to DEFAULT_TOOL_EMOJI, so
# plugin/MCP tools never look broken.
_TOOL_EMOJI: Dict[str, str] = {
    "terminal": "🖥",
    "read_file": "📄",
    "write_file": "📝",
    "patch": "🩹",
    "search_files": "🔎",
    "web_search": "🌐",
    "web_extract": "📥",
    "browser_exec": "🧭",
    "execute_code": "🐍",
    "delegate_task": "🤝",
    "skill_view": "📚",
    "skill_manage": "🧠",
    "memory": "💾",
    "todo_list": "✅",
    "clarify": "❓",
    "image_generation": "🎨",
    "vision_analyze": "👁",
    "text_to_speech": "🔊",
    "napcat_call": "🐧",
}

# Argument keys worth showing, most specific first.  Anything else falls back to
# the first scalar argument, so unknown tools still render something useful.
_PREVIEW_KEYS: Tuple[str, ...] = (
    "command", "cmd", "path", "file_path", "pattern", "query", "url",
    "goal", "name", "prompt", "text", "skill", "model",
)


# Separator between the buffered tool lines and the agent's own text when they
# share one message (plan A: one bubble instead of a progress bubble plus the
# sentence). Box-drawing U+2500 on purpose: ``strip_markdown()`` rewrites ASCII
# ``---`` into its own horizontal rule and would fight a hand-made one, while
# this glyph passes through untouched and matches the borders formatting.py
# already draws for code blocks.
PROGRESS_SEPARATOR = "──────────"


def merge_progress_lines(lines: List[str], text: str) -> str:
    """Put buffered tool *lines* above *text* in ONE message.

    Returns *text* unchanged when there is nothing buffered, and the lines alone
    when *text* is blank — the caller decides whether that is worth sending.
    """
    if not lines:
        return text
    if not text or not text.strip():
        return "\n".join(lines)
    return "\n".join([*lines, PROGRESS_SEPARATOR, text])


def truthy(value: Any) -> bool:
    """Config bool-or-string -> bool (``""`` / ``"0"`` / ``"false"`` are False)."""
    if isinstance(value, bool):
        return value
    return isinstance(value, str) and value.strip().lower() in {"1", "true", "yes", "on"}


def _shorten(text: str, limit: int) -> str:
    """Collapse whitespace and truncate to *limit* characters with an ellipsis."""
    collapsed = " ".join(str(text).split())
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[: max(1, limit - 1)] + "…"


def _first_scalar(args: Dict[str, Any]) -> str:
    """First non-empty scalar value in *args* (dict/list values are skipped)."""
    for value in args.values():
        if isinstance(value, (str, int, float)) and str(value).strip():
            return str(value)
    return ""


def summarize_args(args: Optional[Any]) -> str:
    """One-line argument preview for a tool call; ``""`` when there is nothing to say."""
    if not isinstance(args, dict) or not args:
        return ""
    for key in _PREVIEW_KEYS:
        value = args.get(key)
        if isinstance(value, (str, int, float)) and str(value).strip():
            return _shorten(str(value), MAX_ARG_PREVIEW)
    return _shorten(_first_scalar(args), MAX_ARG_PREVIEW)


def format_tool_line(tool_name: Optional[str], args: Optional[Any] = None) -> str:
    """Render one buffered progress line, e.g. ``🖥 terminal · git status``.

    Plain text only (QQ does not render Markdown) and single-line by
    construction: a batch is joined with newlines, so a line break inside one
    entry would break the one-line-per-call reading.
    """
    name = _shorten(tool_name or "tool", 40) or "tool"
    emoji = _TOOL_EMOJI.get(name, DEFAULT_TOOL_EMOJI)
    preview = summarize_args(args)
    line = f"{emoji} {name} · {preview}" if preview else f"{emoji} {name}"
    return _shorten(line, MAX_LINE_LEN)


class ProgressBuffer:
    """Pending progress lines for one chat, with the window's start timestamp.

    The adapter owns the timer; this only tracks state, so every policy
    decision stays inspectable in tests.
    """

    def __init__(self) -> None:
        self._lines: List[str] = []
        self._chat_id: Optional[str] = None
        self._first_ts: float = 0.0

    def __len__(self) -> int:
        return len(self._lines)

    @property
    def empty(self) -> bool:
        return not self._lines

    @property
    def chat_id(self) -> Optional[str]:
        """Chat the pending lines belong to (``None`` when empty)."""
        return self._chat_id

    @property
    def first_ts(self) -> float:
        """``time.monotonic()`` stamp of the first pending line (0.0 when empty)."""
        return self._first_ts

    def age(self, now: float) -> float:
        """Seconds since the first pending line was added (0.0 when empty)."""
        return 0.0 if not self._lines else max(0.0, now - self._first_ts)

    def add(self, chat_id: str, line: str, now: float) -> None:
        """Append *line* for *chat_id*; a new chat resets the buffer.

        The adapter is one QQ account with a single active turn at a time
        (``_typing_chat_id``), so switching chats mid-buffer means the previous
        turn is over — its lines must not leak into the next turn's message.
        """
        if self._chat_id != chat_id:
            self._lines = []
            self._chat_id = chat_id
            self._first_ts = now
        if not self._lines:
            self._first_ts = now
        self._lines.append(line)

    def take(self) -> Tuple[Optional[str], List[str]]:
        """Return ``(chat_id, lines)`` and reset the buffer."""
        chat_id, lines = self._chat_id, self._lines
        self._lines = []
        self._chat_id = None
        self._first_ts = 0.0
        return chat_id, lines

    def clear(self) -> None:
        """Drop everything pending (turn over / disconnect)."""
        self._lines = []
        self._chat_id = None
        self._first_ts = 0.0
