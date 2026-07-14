import os
import re
from typing import List

ANSI_ESCAPE = re.compile(r'\x1b\[[0-9;]*[mK]')


def supports_color(stream) -> bool:
    if "FORCE_COLOR" in os.environ:
        return True
    if "NO_COLOR" in os.environ:
        return False
    if os.environ.get("TERM") == "dumb":
        return False
    if stream is None:
        return False
    return getattr(stream, "isatty", lambda: False)()


class Formatter:
    def __init__(self, enabled: bool = False) -> None:
        self.enabled = enabled

    def bold(self, text: str) -> str:
        return f"\x1b[1m{text}\x1b[0m" if self.enabled else text

    def dim(self, text: str) -> str:
        return f"\x1b[2m{text}\x1b[0m" if self.enabled else text

    def ok(self, text: str) -> str:
        return f"\x1b[32m{text}\x1b[0m" if self.enabled else text

    def warn(self, text: str) -> str:
        return f"\x1b[33m{text}\x1b[0m" if self.enabled else text

    def err(self, text: str) -> str:
        return f"\x1b[31m{text}\x1b[0m" if self.enabled else text

    def accent(self, text: str) -> str:
        return f"\x1b[36m{text}\x1b[0m" if self.enabled else text

    def header(self, text: str) -> str:
        return f"\x1b[1;35m{text}\x1b[0m" if self.enabled else text


def _visual_len(text: str) -> int:
    return len(ANSI_ESCAPE.sub('', text))


def _truncate_cell(cell: str, max_col: int) -> str:
    vlen = _visual_len(cell)
    if vlen <= max_col:
        return cell

    target_limit = max_col - 3
    if target_limit < 0:
        target_limit = 0

    res = []
    curr_vlen = 0
    i = 0
    n = len(cell)
    while i < n:
        if cell[i] == '\x1b':
            match = ANSI_ESCAPE.match(cell, i)
            if match:
                res.append(match.group(0))
                i = match.end()
                continue
        if curr_vlen < target_limit:
            res.append(cell[i])
            curr_vlen += 1
            i += 1
        else:
            break

    res.append("...")
    if any(char == '\x1b' for char in cell):
        res.append("\x1b[0m")
    return "".join(res)


def render_table(headers: List[str], rows: List[List[str]], max_col: int = 48) -> List[str]:
    if not headers and not rows:
        return []

    num_cols = len(headers) if headers else (len(rows[0]) if rows else 0)
    if num_cols == 0:
        return []

    trunc_headers = [_truncate_cell(h, max_col) for h in headers]
    trunc_rows = [[_truncate_cell(cell, max_col) for cell in row] for row in rows]

    col_widths = []
    for c in range(num_cols):
        widths = []
        if trunc_headers:
            widths.append(_visual_len(trunc_headers[c]))
        for row in trunc_rows:
            if c < len(row):
                widths.append(_visual_len(row[c]))
        col_widths.append(max(widths) if widths else 0)

    lines = []
    if trunc_headers:
        header_line = []
        for c, h in enumerate(trunc_headers):
            pad = col_widths[c] - _visual_len(h)
            header_line.append(h + " " * pad)
        lines.append("  ".join(header_line).rstrip())

    for row in trunc_rows:
        row_line = []
        for c in range(num_cols):
            cell = row[c] if c < len(row) else ""
            pad = col_widths[c] - _visual_len(cell)
            row_line.append(cell + " " * pad)
        lines.append("  ".join(row_line).rstrip())

    return lines
