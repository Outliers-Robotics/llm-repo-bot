"""Prepare standard Markdown for Slack's native Markdown message field."""

import re


MAX_MARKDOWN_LENGTH = 12_000
_FENCE = re.compile(r"^ {0,3}(```|~~~)([A-Za-z0-9_+.#-]{0,32})[ \t]*$")


def _open_fence(text: str) -> str | None:
    opening = None
    for line in text.splitlines():
        match = _FENCE.fullmatch(line)
        if not match:
            continue
        if opening is None:
            opening = match.group(1) + match.group(2)
        elif match.group(1) == opening[:3] and not match.group(2).strip():
            opening = None
    return opening


def split_markdown(text: str) -> list[str]:
    """Split long replies without truncating them; continue fenced code blocks.

    Keep normal replies intact. For longer answers, prefer paragraph boundaries,
    then line boundaries, and only split a line if it cannot fit in one message.
    """
    remaining = text.strip()
    messages = []
    prefix = ""
    while len(prefix) + len(remaining) > MAX_MARKDOWN_LENGTH:
        # Reserve space to close a code fence at the message boundary.
        room = MAX_MARKDOWN_LENGTH - len(prefix) - 4
        boundary = remaining.rfind("\n\n", 0, room)
        if boundary >= 0:
            boundary += 2
        else:
            boundary = remaining.rfind("\n", 0, room) + 1
        if boundary <= 0:
            boundary = room
        part = prefix + remaining[:boundary]
        remaining = remaining[boundary:]
        fence = _open_fence(part)
        if fence:
            part += ("" if part.endswith("\n") else "\n") + fence[:3]
        messages.append(part)
        prefix = fence + "\n" if fence else ""
    if remaining:
        messages.append(prefix + remaining)
    return messages
