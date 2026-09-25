"""A terminal recording, read back as lines.

`script(1)` records bytes, not text: an agent CLI draws a screen, moves the
cursor around it, and redraws. Stripping the escape codes out of that gives
fragments spliced together in the order they happened to be painted, which is
what made the transcript unreadable in a text box and scrambled in a replay at
the wrong width.

So this replays the recording the way a terminal would — a grid, a cursor, the
handful of control sequences that move it — and emits each row as it scrolls
off the top. That is the scrollback, which is the part a human means by "the
log". The recording on disk is never touched; this only reads it.

Times come from `script --log-timing`, which records how long the pause before
each chunk of output was. Recordings made before that flag was added replay
fine and come back with empty timestamps, because inventing one would be worse
than admitting there is none.

ponytail: handles the sequences an agent CLI actually emits (cursor moves,
erases, scroll region ignored), not the VT spec. A sequence it does not know is
dropped, which costs a little layout fidelity and never corrupts a line. If a
CLI shows up that needs more, reach for pyte rather than growing this.
"""

from __future__ import annotations

import datetime
import re
from pathlib import Path

#: the header `script` writes, which carries the width the run was recorded at
_HEADER = re.compile(rb'COLUMNS="(\d+)" LINES="(\d+)"')
_CSI = re.compile(rb"\x1b\[([0-9;?]*)([ -/]*)([@-~])")
_OSC = re.compile(rb"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")


def geometry(raw: bytes, default: tuple[int, int] = (80, 24)) -> tuple[int, int]:
    """Columns and rows the recording was made at. Replaying at any other width
    is what turns a clean transcript into interleaved fragments."""
    found = _HEADER.search(raw[:4096])
    if not found:
        return default
    cols, rows = int(found.group(1)), int(found.group(2))
    return (cols if 20 <= cols <= 500 else default[0],
            rows if 5 <= rows <= 200 else default[1])


class _Screen:
    """Grid, cursor, and a list of rows that have scrolled out of sight."""

    def __init__(self, cols: int, rows: int):
        self.cols, self.rows = cols, rows
        self.grid = [[" "] * cols for _ in range(rows)]
        self.x = self.y = 0
        self.out: list[tuple[float, str]] = []
        self.clock = 0.0

    def _emit(self, row: list[str]) -> None:
        text = "".join(row).rstrip()
        if not text:
            # one blank line between blocks, never a screenful of them
            if self.out and self.out[-1][1] == "":
                return
        elif self.out and self.out[-1][1] == text:
            return  # a redraw of the line we just kept
        self.out.append((self.clock, text))

    def newline(self) -> None:
        if self.y + 1 < self.rows:
            self.y += 1
            return
        self._emit(self.grid.pop(0))
        self.grid.append([" "] * self.cols)

    def write(self, text: str) -> None:
        for ch in text:
            if self.x >= self.cols:
                self.x = 0
                self.newline()
            self.grid[self.y][self.x] = ch
            self.x += 1

    def erase_line(self, mode: int) -> None:
        row = self.grid[self.y]
        span = (range(self.x, self.cols) if mode == 0
                else range(0, self.x + 1) if mode == 1 else range(self.cols))
        for i in span:
            row[i] = " "

    def erase_display(self, mode: int) -> None:
        rows = (range(self.y + 1, self.rows) if mode == 0
                else range(0, self.y) if mode == 1 else range(self.rows))
        if mode in (2, 3):
            # a full clear throws the screen away; keep what was on it, since
            # `clear` between turns is how a TUI starts a new one
            for row in self.grid:
                self._emit(row)
        for i in rows:
            self.grid[i] = [" "] * self.cols
        if mode in (2, 3):
            self.x = self.y = 0
        else:
            self.erase_line(mode)

    def flush(self) -> list[tuple[float, str]]:
        for row in self.grid:
            self._emit(row)
        while self.out and self.out[-1][1] == "":
            self.out.pop()
        return self.out


def _chunks(raw: bytes, timing: bytes | None):
    """(delay_seconds, bytes) pairs. With no timing file the whole recording is
    one chunk with no delay, which is how an old log ends up unstamped."""
    if not timing:
        yield 0.0, raw
        return
    at = 0
    for line in timing.splitlines():
        parts = line.split()
        # classic: "<delay> <count>"; advanced: "<stream> <delay> <count>"
        if len(parts) == 3 and parts[0] in (b"O", b"I"):
            stream, delay, count = parts
        elif len(parts) == 2:
            stream, (delay, count) = b"O", parts
        else:
            continue
        try:
            delay_s, size = float(delay), int(count)
        except ValueError:
            continue
        piece = raw[at:at + size]
        at += size
        if stream == b"O":
            yield delay_s, piece
    if at < len(raw):
        yield 0.0, raw[at:]


def lines(path: str | Path, limit: int = 4000) -> list[dict]:
    """The recording as `[{"ts": "14:02:31", "text": "…"}]`, oldest first.

    `ts` is wall clock when the recording carries timing, and empty when it does
    not. Only the last `limit` lines come back: the answer to "what happened"
    lives at the end of a run, and a 100k-line log helps nobody get to it.
    """
    path = Path(path)
    raw = path.read_bytes()
    timing_file = path.with_suffix(path.suffix + ".timing")
    timing = timing_file.read_bytes() if timing_file.exists() else None
    cols, rows = geometry(raw)
    screen = _Screen(cols, rows)

    for delay, piece in _chunks(raw, timing):
        screen.clock += delay
        text = _OSC.sub(b"", piece)
        pos = 0
        while pos < len(text):
            found = _CSI.search(text, pos)
            plain = text[pos:found.start()] if found else text[pos:]
            for part in re.split(rb"([\r\n\x08\t])", plain):
                if part == b"\r":
                    screen.x = 0
                elif part == b"\n":
                    screen.newline()
                elif part == b"\x08":
                    screen.x = max(0, screen.x - 1)
                elif part == b"\t":
                    screen.x = min(screen.cols - 1, (screen.x // 8 + 1) * 8)
                elif part:
                    clean = re.sub(rb"[\x00-\x1f\x7f]", b"", part)
                    screen.write(clean.decode("utf-8", "replace"))
            if not found:
                break
            pos = found.end()
            _apply(screen, found.group(1), found.group(3))

    out = screen.flush()[-limit:]
    # the file's last write is the run's end, so the elapsed clock counts back
    # from there to a real time of day
    total = out[-1][0] if out else 0.0
    end = datetime.datetime.fromtimestamp(path.stat().st_mtime)
    return [{"ts": "" if timing is None
                   else (end - datetime.timedelta(seconds=total - at)).strftime("%H:%M:%S"),
             "text": text} for at, text in out]


def _apply(screen: _Screen, params: bytes, final: bytes) -> None:
    nums = [int(p) if p.isdigit() else 0 for p in params.split(b";") if p[:1] != b"?"]
    first = nums[0] if nums else 0
    if final == b"m":
        return  # colour: the line log is text, and the terminal view keeps it
    if final in (b"H", b"f"):
        screen.y = min(max((nums[0] if nums else 1) - 1, 0), screen.rows - 1)
        screen.x = min(max((nums[1] if len(nums) > 1 else 1) - 1, 0), screen.cols - 1)
    elif final == b"A":
        screen.y = max(0, screen.y - max(1, first))
    elif final == b"B":
        screen.y = min(screen.rows - 1, screen.y + max(1, first))
    elif final == b"C":
        screen.x = min(screen.cols - 1, screen.x + max(1, first))
    elif final == b"D":
        screen.x = max(0, screen.x - max(1, first))
    elif final == b"G":
        screen.x = min(max(first - 1, 0), screen.cols - 1)
    elif final == b"K":
        screen.erase_line(first)
    elif final == b"J":
        screen.erase_display(first)


def demo() -> None:
    """Self-check: a repaint must not become two lines, and a carriage return
    must overwrite rather than splice."""
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        log = Path(tmp) / "t.log"
        log.write_bytes(
            b'Script started COLUMNS="40" LINES="6"\n'
            b"hello\r\n"
            b"working...\rworking done\r\n"          # CR repaint, not two lines
            b"\x1b[31mred\x1b[0m line\r\n"            # colour is dropped
            b"one\r\n\x1b[Atwo\r\n"                   # cursor up then rewrite
            b"tail\r\n")
        got = [row["text"] for row in lines(log)]
        assert "working done" in got, got
        assert not any("working..." == g for g in got), got
        assert "red line" in got, got
        assert "two" in got and "one" not in got, got   # the redraw won
        assert all(row["ts"] == "" for row in lines(log))  # no timing, no times

        # with a timing file every line carries a wall clock
        log.with_suffix(".log.timing").write_bytes(b"0.5 38\n1.0 %d\n" % (log.stat().st_size - 38))
        stamped = lines(log)
        assert all(len(row["ts"]) == 8 for row in stamped), stamped

        assert geometry(b'x COLUMNS="120" LINES="40"\n') == (120, 40)
        assert geometry(b"nothing here") == (80, 24)
    print("translog self-check ok")


if __name__ == "__main__":
    demo()
