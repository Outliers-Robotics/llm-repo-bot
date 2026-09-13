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


_LATEX_MATH_SYMBOLS = {
    r"\approx": "≈",
    r"\le": "≤",
    r"\leq": "≤",
    r"\ge": "≥",
    r"\geq": "≥",
    r"\ne": "≠",
    r"\neq": "≠",
    r"\pm": "±",
    r"\times": "×",
    r"\div": "÷",
    r"\cdot": "·",
    r"^\circ": "°",
    r"\circ": "°",
    r"\degree": "°",
    r"\Delta": "Δ",
    r"\delta": "δ",
    r"\theta": "θ",
    r"\pi": "π",
    r"\mu": "μ",
    r"\alpha": "α",
    r"\beta": "β",
    r"\omega": "ω",
    r"\Omega": "Ω",
    r"\infty": "∞",
    r"\to": "→",
    r"\rightarrow": "→",
    r"\leftarrow": "←",
    r"\sim": "~",
    r"\dots": "...",
    r"\ldots": "...",
}

_SUPERSCRIPTS = str.maketrans("0123456789+-=()n", "⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻⁼⁽⁾ⁿ")
_SUBSCRIPTS = str.maketrans("0123456789+-=()aehklmnoprstuvx", "₀₁₂₃₄₅₆₇₈₉₊₋₌₍₎ₐₑₕₖₗₘₙₒₚᵣₛₜᵤᵥₓ")
_BLOCK_MATH_PATTERN = re.compile(
    r"\$\$([\s\S]+?)\$\$|" + re.escape(r"\[") + r"([\s\S]+?)" + re.escape(r"\]")
)
_INLINE_MATH_PATTERN = re.compile(
    r"\$(?!\s)([^$\n]+?)(?<!\s)\$|" + re.escape(r"\(") + r"([\s\S]+?)" + re.escape(r"\)")
)


def _clean_latex_inner(expr: str) -> str:
    expr = re.sub(r"\\(?:left|right)\s*(?=[()\[\]{}|])", "", expr)
    expr = re.sub(r"\\(?:quad|qquad|\s|[,;:])", " ", expr)
    expr = re.sub(r"\\!", "", expr)
    for _ in range(3):
        expr = re.sub(r"\\(?:text|mathrm|mathbf|mathit|textbf|textit)\{([^{}]+)\}", r"\1", expr)

    def frac_repl(match):
        num, den = match.group(1).strip(), match.group(2).strip()
        num_str = f"({num})" if any(c in num for c in " +-") else num
        den_str = f"({den})" if any(c in den for c in " +-") else den
        return f"{num_str} / {den_str}"

    for _ in range(3):
        expr = re.sub(r"\\frac\{([^{}]+)\}\{([^{}]+)\}", frac_repl, expr)
    expr = re.sub(r"\\sqrt\{([^{}]+)\}", r"√(\1)", expr)
    for sym, repl in _LATEX_MATH_SYMBOLS.items():
        expr = expr.replace(sym, repl)
    expr = re.sub(r"\^\{([0-9+-=()n]+)\}", lambda m: m.group(1).translate(_SUPERSCRIPTS), expr)
    expr = re.sub(r"\^([0-9n])", lambda m: m.group(1).translate(_SUPERSCRIPTS), expr)
    expr = re.sub(r"_\{([0-9+-=()aehklmnoprstuvx]+)\}", lambda m: m.group(1).translate(_SUBSCRIPTS), expr)
    expr = re.sub(r"_([0-9])", lambda m: m.group(1).translate(_SUBSCRIPTS), expr)
    expr = re.sub(r"_\{([^}]+)\}", r"_\1", expr)
    expr = re.sub(r"\\([A-Za-z]+)", r"\1", expr)
    return expr.strip()


def format_math_for_slack(text: str) -> str:
    """Convert LaTeX math syntax to clean plaintext and Unicode for Slack."""
    pattern = re.compile(r"(```[\s\S]*?```|`[^`\n]+?`)")
    parts = pattern.split(text)
    for i in range(0, len(parts), 2):
        parts[i] = _BLOCK_MATH_PATTERN.sub(
            lambda m: _clean_latex_inner(m.group(1) or m.group(2)),
            parts[i],
        )
        parts[i] = _INLINE_MATH_PATTERN.sub(
            lambda m: _clean_latex_inner(m.group(1) or m.group(2)),
            parts[i],
        )
    return "".join(parts)


def split_markdown(text: str) -> list[str]:
    """Split long replies without truncating them; continue fenced code blocks.

    Keep normal replies intact. For longer answers, prefer paragraph boundaries,
    then line boundaries, and only split a line if it cannot fit in one message.
    """
    text = format_math_for_slack(text)
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
