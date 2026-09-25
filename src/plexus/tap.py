"""Record a terminal session's output, with timing, from a pipe.

`script(1)` records a command it starts. A conversation with an agent is not
started that way — it lives in a tmux session you can attach to from anywhere —
so tmux taps it with `pipe-pane`, and the tap needs somewhere to put the bytes.

Two files, both append-only: `<log>` gets the bytes exactly as the pane emitted
them, `<log>.timing` gets one `<delay> <count>` line per chunk, which is
`script`'s own classic format. `translog.py` reads either pair without caring
which produced it.

Usage: tmux pipe-pane -o 'python3 -m plexus.tap /path/to/session.log'
"""

from __future__ import annotations

import sys
import time
from pathlib import Path


def tap(path: str | Path, stream=None) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    timing = path.with_suffix(path.suffix + ".timing")
    source = stream if stream is not None else sys.stdin.buffer
    last = time.time()
    # line buffering would split escape sequences across writes; append whole
    # chunks and flush, so a reader never sees half a sequence
    with open(path, "ab", buffering=0) as log, open(timing, "a", buffering=1) as clock:
        while True:
            chunk = source.read1(65536) if hasattr(source, "read1") else source.read(65536)
            if not chunk:
                return
            now = time.time()
            log.write(chunk)
            clock.write(f"{now - last:.6f} {len(chunk)}\n")
            last = now


def demo() -> None:
    import io
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "s.log"
        tap(out, io.BytesIO(b"hello\r\nworld\r\n"))
        assert out.read_bytes() == b"hello\r\nworld\r\n"
        delay, count = out.with_suffix(".log.timing").read_text().split()
        assert int(count) == 14 and float(delay) >= 0
        from . import translog
        assert [r["text"] for r in translog.lines(out)] == ["hello", "world"]
    print("tap self-check ok")


if __name__ == "__main__":
    if len(sys.argv) > 1:
        tap(sys.argv[1])
    else:
        demo()
