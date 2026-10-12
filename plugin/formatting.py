"""QQ plain-text formatting (QQ does not render Markdown) and OneBot 11 segment helpers.

Outbound: :func:`strip_markdown` converts the agent's Markdown reply into clean
plain text (fenced code becomes bordered blocks, headings become 【】/▌ markers,
tables collapse to rows).  Kept from the original hermes-napcat plugin — the
same convention main's IRC adapter uses for a Markdown-less transport.
"""

import re

QQ_TEXT_LIMIT = 4500  # OneBot has no hard cap; keep generous and chunk safely

# Matches a Markdown fenced-code opener/closer at the start of a line.
_FENCE = re.compile(r"^(?P<ticks>`{3,}|~{3,})(?P<rest>.*)$")


def strip_markdown(text: str) -> str:
    """Convert Markdown to clean QQ-friendly plain text."""
    lines = text.splitlines()
    out: list[str] = []
    in_code = False
    open_ticks = ""
    code_lang = ""
    code_lines: list[str] = []

    def _flush_code() -> None:
        label = f"[{code_lang}]" if code_lang else "[代码]"
        out.append(f"┌─{label}─")
        for cl in code_lines:
            out.append("│ " + cl)
        out.append("└──────")
        code_lines.clear()

    for line in lines:
        m = _FENCE.match(line.strip())
        if m and not in_code:
            in_code = True
            open_ticks = m.group("ticks")
            code_lang = m.group("rest").strip()
            continue
        if m and in_code:
            # Close only on a run of the SAME character at least as long as
            # the opener (standard Markdown); anything else is code content.
            if m.group("ticks")[0] == open_ticks[0] and len(m.group("ticks")) >= len(open_ticks):
                in_code = False
                _flush_code()
                continue
            code_lines.append(line)
            continue
        if in_code:
            code_lines.append(line)
            continue

        h = re.match(r"^(#{1,6})\s+(.*)", line)
        if h:
            level, title = len(h.group(1)), h.group(2).strip()
            out.append(f"【{_inline(title)}】" if level <= 2 else f"▌ {_inline(title)}")
            continue

        if re.match(r"^\s*[-*_]{3,}\s*$", line):
            out.append("────────────────")
            continue

        bq = re.match(r"^>\s?(.*)", line)
        if bq:
            out.append("「" + _inline(bq.group(1)) + "」")
            continue

        ul = re.match(r"^(\s*)[-*+]\s+(.*)", line)
        if ul:
            indent = len(ul.group(1)) // 2
            out.append("  " * indent + "• " + _inline(ul.group(2)))
            continue

        ol = re.match(r"^(\s*)\d+[.)]\s+(.*)", line)
        if ol:
            indent = len(ol.group(1)) // 2
            num = re.match(r"^\s*(\d+)", line).group(1)
            out.append("  " * indent + num + ". " + _inline(ol.group(2)))
            continue

        if re.match(r"^\s*\|", line):
            if re.match(r"^\s*\|[\s\-:|]+\|\s*$", line):
                continue
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            out.append("  ".join(_inline(c) for c in cells if c))
            continue

        out.append(_inline(line))

    # Unterminated fence: flush whatever we collected instead of dropping it.
    if in_code:
        _flush_code()

    return "\n".join(out).strip()


def _inline(text: str) -> str:
    """Strip inline Markdown from a single line."""
    text = re.sub(r"`([^`\n]+)`", r"\1", text)
    text = re.sub(r"\*{3}(.+?)\*{3}", r"\1", text)
    text = re.sub(r"_{3}(.+?)_{3}", r"\1", text)
    text = re.sub(r"\*{2}(.+?)\*{2}", r"\1", text)
    text = re.sub(r"_{2}(.+?)_{2}", r"\1", text)
    text = re.sub(r"\*(.+?)\*", r"\1", text)
    text = re.sub(r"(?<!\w)_(.+?)_(?!\w)", r"\1", text)
    text = re.sub(r"~~(.+?)~~", r"\1", text)
    # Image first — the link rule below would otherwise leave a stray "!alt".
    text = re.sub(r"!\[([^\]]*)\]\([^)]+\)", r"[\1]", text)
    text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"\1（\2）", text)
    text = re.sub(r"\[([^\]]+)\]\[[^\]]*\]", r"\1", text)
    return text
