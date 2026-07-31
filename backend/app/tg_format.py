"""Small, parse-mode-free building blocks for Telegram message layout.

Telegram's default font is not reliably monospaced across clients, so these
helpers create hierarchy with box-drawing characters instead of padded table
columns.  The output stays plain text: dynamic names, emails and API errors do
not need Markdown/HTML escaping.
"""

from collections.abc import Iterable


_PANEL_RULE = "────────────────────"


def detail_card(title: str, rows: Iterable[str]) -> str:
    """Render a title followed by tree-style detail rows."""
    values = [str(row).strip() for row in rows if str(row).strip()]
    if not values:
        return str(title)
    lines = [str(title)]
    last = len(values) - 1
    lines.extend(
        f"{'└' if index == last else '├'} {value}"
        for index, value in enumerate(values)
    )
    return "\n".join(lines)


def overview_panel(rows: Iterable[str], *, label: str = "概览") -> str:
    """Render a compact summary panel without relying on column alignment."""
    values = [str(row).strip() for row in rows if str(row).strip()]
    return "\n".join(
        [f"╭─ {label}", *(f"│ {value}" for value in values), f"╰{_PANEL_RULE}"]
    )
