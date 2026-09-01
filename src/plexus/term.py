"""Browser terminals, one persistent shell per project.

Each project gets a tmux session named for its directory (`plexus-plexus`),
opened through a PTY this server owns. tmux — not a bare shell — because it is the only reason
the session outlives anything: close the tab, restart this server, and
`tmux new-session -A` re-attaches to the same shell with its scrollback intact.
It also means the browser terminal and `tmux attach -t plexus-plexus` from a real
terminal are the *same* session, so moving between them loses nothing.

Transport is SSE out and POST in, both plain stdlib. `http.server` speaks no
WebSocket, and on loopback a POST per keystroke costs less than the frame codec
would have cost to write and maintain.
"""

from __future__ import annotations

import base64
import errno
import hashlib
import fcntl
import os
import pty
import shlex
import signal
import struct
import subprocess
import termios
import threading
import time
import re
from collections import deque
from pathlib import Path
from queue import Empty, Queue

# Replayed to a client the moment it connects, so a page reload redraws the
# screen instead of showing a blank pane until the next keystroke.
SCROLLBACK_BYTES = 256 * 1024

#: Questions a terminal is expected to answer: primary/secondary device
#: attributes, XTVERSION, and DECRQM mode reports.
_CAPABILITY_QUERY = re.compile(rb"\x1b\[(?:>?c|>q|\?[0-9;]*\$p)")
#: How long a fresh tmux client spends probing the terminal it attached to.
_HANDSHAKE_S = 2.0
_IDLE_TIMEOUT_S = 60 * 60


class Session:
    """One PTY, many viewers. Output fans out to every subscriber queue."""

    def __init__(self, name: str, cwd: Path, cols: int = 120, rows: int = 32):
        self.name = name
        self.cwd = cwd
        self.lock = threading.Lock()
        # separate from `lock`: a write must not block on the reader's
        # bookkeeping, and the reader must not block behind a slow write
        self.write_lock = threading.Lock()
        self.subscribers: list[Queue] = []
        self.scrollback: deque[bytes] = deque()
        self.scrollback_len = 0
        self.last_seen = time.time()
        self.born = time.time()
        self.fd, slave = pty.openpty()
        _set_size(self.fd, cols, rows)
        # `-A` attaches to the existing session or creates it. Deliberately no
        # `-D`: detaching other clients meant the browser and a real `tmux
        # attach` kicked each other off in a loop, and every re-attach makes
        # tmux re-query terminal capabilities — which is where the stream of
        # `1;2c0;276;0c` came from. Sharing the session is the whole promise;
        # a flag that evicts the other viewer breaks it.
        # `start_new_session=True` would call setsid() and leave the client with
        # no controlling terminal — so the kernel never delivers SIGWINCH and
        # the pane stays at whatever size the first viewer happened to ask for,
        # forever. Claim the slave as the controlling tty instead: setsid, then
        # TIOCSCTTY on fd 0, which is the slave.
        def _own_tty() -> None:
            os.setsid()
            fcntl.ioctl(0, termios.TIOCSCTTY, 0)

        self.proc = subprocess.Popen(
            ["tmux", "new-session", "-A", "-s", name, "-c", str(cwd)],
            stdin=slave, stdout=slave, stderr=slave,
            preexec_fn=_own_tty,
            env={**os.environ, "TERM": "xterm-256color"},
        )
        os.close(slave)
        configure(name)
        self.reader = threading.Thread(target=self._pump, daemon=True)
        self.reader.start()

    def _pump(self) -> None:
        while True:
            try:
                chunk = os.read(self.fd, 65536)
            except OSError as exc:
                # EIO is the normal end: the child closed the last slave fd.
                if exc.errno not in (errno.EIO, errno.EBADF):
                    raise
                chunk = b""
            if not chunk:
                break
            # tmux probes the terminal the instant it attaches, which is before
            # any browser is listening. The question therefore reaches the page
            # late — as live output on a first connection, or replayed from
            # scrollback on a later one — and the answer arrives long after tmux
            # stopped waiting, so it falls through to the shell, which prints it
            # as `1;2c0;276;0c` into whatever you were typing.
            #
            # Dropping the probe for the length of the handshake stops the
            # question ever being asked of the browser. tmux falls back to what
            # TERM already tells it, which is what it was using anyway.
            #
            # ponytail: a time window, not a protocol. A program started inside
            # the first two seconds of a session could lose one query; every
            # later one passes untouched, which is what matters — a TUI asking
            # what it is talking to must get an answer.
            if time.time() - self.born < _HANDSHAKE_S:
                chunk = _CAPABILITY_QUERY.sub(b"", chunk)
                if not chunk:
                    continue
            with self.lock:
                self.scrollback.append(chunk)
                self.scrollback_len += len(chunk)
                while self.scrollback_len > SCROLLBACK_BYTES:
                    self.scrollback_len -= len(self.scrollback.popleft())
                for queue in list(self.subscribers):
                    queue.put(chunk)
        with self.lock:
            for queue in list(self.subscribers):
                queue.put(None)

    def subscribe(self) -> tuple[Queue, bytes]:
        queue: Queue = Queue()
        with self.lock:
            self.subscribers.append(queue)
            self.last_seen = time.time()
            return queue, b"".join(self.scrollback)

    def unsubscribe(self, queue: Queue) -> None:
        with self.lock:
            if queue in self.subscribers:
                self.subscribers.remove(queue)
            self.last_seen = time.time()

    def write(self, data: bytes) -> None:
        """Write every byte, in order, without interleaving another writer.

        `os.write` on a PTY is allowed to write fewer bytes than it was given —
        fine for one keystroke, silently truncating for a paste. And each HTTP
        request runs on its own thread, so two writes could interleave mid
        sequence and turn an escape code into garbage. Both are the kind of bug
        that only shows up when you type quickly, which is exactly when a
        terminal has to be trustworthy.
        """
        with self.lock:
            self.last_seen = time.time()
        with self.write_lock:
            view = memoryview(data)
            while view:
                view = view[os.write(self.fd, view):]

    def resize(self, cols: int, rows: int) -> None:
        _set_size(self.fd, cols, rows)

    def alive(self) -> bool:
        return self.proc.poll() is None

    def close(self) -> None:
        """Drop our PTY. The tmux session itself keeps running on purpose — it
        is the thing the user can attach to from a real terminal, and killing it
        here would throw away the work a page reload is supposed to preserve."""
        if self.proc.poll() is None:
            try:
                os.killpg(os.getpgid(self.proc.pid), signal.SIGTERM)
            except (OSError, ProcessLookupError):
                pass
        try:
            os.close(self.fd)
        except OSError:
            pass


def _set_size(fd: int, cols: int, rows: int) -> None:
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))


_sessions: dict[str, Session] = {}
_sessions_lock = threading.Lock()


def available() -> bool:
    """tmux is the whole persistence story, so a terminal without it is not
    worth offering — the UI hides the tab rather than handing out a shell that
    silently loses everything on reload."""
    return bool(_which("tmux"))


def _which(name: str) -> str | None:
    from shutil import which
    return which(name)


def _tmux(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["tmux", *args], capture_output=True, text=True)


#: Session-scoped only — never `set -g`. These make a tmux session behave like
#: a plain terminal in a web page, and writing them globally would reach into
#: the user's own tmux server and change every session they have open.
_LOOKS_LIKE_A_TERMINAL = (
    ("mouse", "on"),          # scroll, click to position, select — without a prefix key
    ("status", "off"),        # the green bar is the whole "you must know tmux" tell
    ("escape-time", "10"),    # ESC is a keystroke here, not the start of a sequence
    ("history-limit", "50000"),
)


def configure(name: str) -> None:
    """Make one session unobtrusive.

    tmux is here for persistence, not for its interface: nobody should have to
    know a prefix key to use a terminal on a web page. With the status bar off
    and the mouse on, what is left looks and behaves like a terminal, and tmux
    only becomes visible if you go looking for it by attaching.
    """
    for option, value in _LOOKS_LIKE_A_TERMINAL:
        # plain name, no `=` prefix: set-option rejects the exact-match form
        # that has-session accepts. tmux resolves an exact name first anyway,
        # so `plexus-plexus` cannot land on `plexus-plexus-overview`.
        _tmux("set-option", "-t", name, option, value)


def ensure_session(name: str, cwd: Path) -> None:
    """Create the session detached if it isn't there. A run can start long
    before anyone opens the terminal tab, so the session cannot depend on a
    browser having attached first."""
    if _tmux("has-session", "-t", f"={name}").returncode != 0:
        _tmux("new-session", "-d", "-s", name, "-c", str(cwd))
        configure(name)


class TmuxJob:
    """A job running in a tmux window, shaped like Popen for the one method the
    server's reaper uses. `poll()` is None while the window lives, then the exit
    code the wrapper recorded — tmux itself keeps no exit status once a window
    closes, so the command writes its own."""

    def __init__(self, window_id: str, exit_file: Path):
        self.window_id = window_id
        self.exit_file = exit_file
        self._code: int | None = None

    def poll(self) -> int | None:
        if self._code is not None:
            return self._code
        if _tmux("list-panes", "-t", self.window_id).returncode == 0:
            return None
        try:
            self._code = int(self.exit_file.read_text().strip())
        except (OSError, ValueError):
            # window gone with no code recorded: killed, or the shell died
            # before the redirect. Failure is the safe reading.
            self._code = -1
        return self._code

    def kill(self) -> None:
        _tmux("kill-window", "-t", self.window_id)


def run_window(session: str, cwd: Path, name: str, argv: list[str],
               env: dict[str, str], transcript: Path,
               exit_file: Path) -> TmuxJob | None:
    """Launch `argv` in a new window of the project's session.

    The agent CLI gets a real TTY this way, which is the point: you can watch it
    in the browser, attach to it from your own shell, and answer a prompt it
    stops on. Every one of those was impossible when the run was a bare Popen
    with nowhere for its output to go.
    """
    ensure_session(session, cwd)
    transcript.parent.mkdir(parents=True, exist_ok=True)
    exit_file.parent.mkdir(parents=True, exist_ok=True)
    exit_file.unlink(missing_ok=True)
    # Only the vars we actually mean to override travel with the command. A
    # tmux window inherits the tmux *server's* environment, which is whatever
    # shell started it — fine for PATH and HOME, wrong for the fleet caps this
    # server is authoritative about.
    passed = {k: v for k, v in env.items()
              if k.startswith(("HEART_", "PLEXUS_")) or k == "VIRTUAL_ENV"}
    prefix = "env " + " ".join(f"{k}={shlex.quote(v)}" for k, v in passed.items())
    command = " ".join(shlex.quote(a) for a in argv)
    # `script` records from the first byte and still hands the child a TTY.
    # tmux's own pipe-pane can only attach *after* the window exists, so a run
    # that prints immediately loses its opening lines — which is exactly the
    # part you want when a run dies on startup. -e propagates the child's exit
    # status so the code written below is the command's, not script's.
    if _which("script"):
        inner = "script -q -e -f -c {} {}".format(
            shlex.quote(f"{prefix} {command}"), shlex.quote(str(transcript)))
    else:
        inner = f"{prefix} {command}"
    made = _tmux("new-window", "-d", "-t", session, "-n", name, "-c", str(cwd),
                 "-P", "-F", "#{window_id}", "sh", "-c",
                 f"{inner}; printf %s $? > {shlex.quote(str(exit_file))}")
    window_id = made.stdout.strip()
    if made.returncode != 0 or not window_id:
        return None
    if not _which("script"):
        # no recorder: fall back to tmux's tap and accept the opening-line race
        _tmux("pipe-pane", "-t", window_id, "-o",
              f"cat >> {shlex.quote(str(transcript))}")
    return TmuxJob(window_id, exit_file)


def select(session: str, window_id: str) -> bool:
    """Point the session at a window. Every client follows — the browser pane
    and a terminal you have attached show the same thing, which is the whole
    promise of putting runs in this session rather than beside it."""
    return _tmux("select-window", "-t", window_id).returncode == 0


def windows(session: str) -> list[dict]:
    """Every window in the session: the user's shell plus one per live run."""
    listed = _tmux("list-windows", "-t", f"={session}", "-F",
                   "#{window_id}\t#{window_name}\t#{window_active}\t#{window_activity}")
    if listed.returncode != 0:
        return []
    out = []
    for line in listed.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) == 4:
            out.append({"id": parts[0], "name": parts[1],
                        "active": parts[2] == "1", "activity": parts[3]})
    return out


def get(name: str, cwd: Path, cols: int = 120, rows: int = 32) -> Session:
    """The PTY viewing one tmux session, keyed by that session's name.

    A project has several: the shell you work in, and one per drafting
    conversation. They are separate tmux sessions rather than windows of one so
    that each tab can show its own without the tabs fighting over which window
    the shared session is looking at — and so closing a conversation is a
    kill-session, not a hunt for the right window."""
    with _sessions_lock:
        session = _sessions.get(name)
        if session is not None and session.alive():
            # adopt the caller's size: reusing a session must not pin the pane
            # to whatever the first viewer ever asked for
            session.resize(cols, rows)
            return session
        if session is not None:
            session.close()
        _reap_idle()
        session = Session(name, cwd, cols, rows)
        _sessions[name] = session
        return session


def start(name: str, cwd: Path, agent: str, opening: str,
          env: dict[str, str] | None = None) -> bool:
    """Open a detached session running `agent` interactively, then send it
    `opening` as its first message.

    The agent is launched with no arguments on purpose. Passing the prompt as
    argv runs most CLIs in one-shot print mode: they answer once and exit, so
    the conversation could never be returned to — which is why closing and
    reopening appeared to reset everything. Interactive, then typed into, is a
    session you can leave and come back to for as long as you want it.

    `opening` is sent as keystrokes rather than argv, so it is also short by
    construction: it points at a file holding the real prompt instead of
    shipping a few thousand characters through send-keys.

    Returns False if the session already exists — clicking discuss twice should
    rejoin, not start a second one beside it.
    """
    if _tmux("has-session", "-t", f"={name}").returncode == 0:
        return False
    passed = {k: v for k, v in (env or {}).items()
              if k.startswith(("HEART_", "PLEXUS_")) or k == "VIRTUAL_ENV"}
    prefix = ("env " + " ".join(f"{k}={shlex.quote(v)}" for k, v in passed.items())
              if passed else "")
    # a shell after the agent, so quitting it leaves you in the repo rather
    # than destroying the session you were reading
    inner = f"{prefix} {shlex.quote(agent)}".strip() + '; exec "${SHELL:-/bin/bash}"'
    made = _tmux("new-session", "-d", "-s", name, "-c", str(cwd), "sh", "-c", inner)
    if made.returncode != 0:
        return False
    configure(name)
    threading.Thread(target=_send_opening, args=(name, opening), daemon=True).start()
    return True


def _send_opening(name: str, opening: str, timeout: float = 25.0) -> None:
    """Type the first message once the CLI has finished starting up.

    Waiting a fixed couple of seconds does not work: an agent CLI paints a
    splash for a while, and text typed into it is swallowed rather than
    buffered — the conversation then opens with an empty prompt and no idea why.
    There is no portable way to ask a CLI whether it is ready, so this watches
    the pane and sends once it has stopped changing, which is what "finished
    drawing" looks like from outside.

    ponytail: settle-detection, not a real readiness protocol. Good enough for
    every CLI that draws once and waits; a CLI with a spinner would need the
    prompt-marker version.
    """
    deadline = time.time() + timeout
    last, stable = None, 0
    while time.time() < deadline:
        time.sleep(0.4)
        pane = _tmux("capture-pane", "-p", "-t", name).stdout
        if not pane.strip():
            continue
        stable = stable + 1 if pane == last else 0
        last = pane
        if stable >= 2:  # ~1.2s unchanged
            break
    _tmux("send-keys", "-t", name, "-l", opening)
    time.sleep(0.15)  # some CLIs debounce paste before reading Enter
    _tmux("send-keys", "-t", name, "Enter")


def kill(name: str) -> bool:
    """Close a session for good: drop our PTY, then kill the tmux session."""
    with _sessions_lock:
        session = _sessions.pop(name, None)
    if session is not None:
        session.close()
    return _tmux("kill-session", "-t", f"={name}").returncode == 0


def exists(name: str) -> bool:
    return _tmux("has-session", "-t", f"={name}").returncode == 0


def _reap_idle() -> None:
    """Called under _sessions_lock. Drops PTYs nobody has watched for an hour;
    the tmux sessions behind them survive and re-attach on next open."""
    now = time.time()
    for key, session in list(_sessions.items()):
        with session.lock:
            idle = not session.subscribers and now - session.last_seen > _IDLE_TIMEOUT_S
        if idle or not session.alive():
            session.close()
            _sessions.pop(key, None)


def stream(session: Session, write, keepalive: float = 15.0) -> None:
    """Blocking SSE loop. `write` takes bytes and may raise on a dropped client.

    PTY output is arbitrary bytes and SSE is a line-oriented text protocol, so
    every chunk goes out base64-encoded rather than hoping no newline or invalid
    UTF-8 ever appears mid-escape-sequence.
    """
    queue, backlog = session.subscribe()
    try:
        if backlog:
            write(_sse(backlog))
        while True:
            try:
                chunk = queue.get(timeout=keepalive)
            except Empty:
                write(b": keepalive\n\n")  # holds proxies and idle sockets open
                continue
            if chunk is None:
                write(b"event: exit\ndata: \n\n")
                return
            write(_sse(chunk))
    finally:
        session.unsubscribe(queue)


def _sse(chunk: bytes) -> bytes:
    return b"data: " + base64.b64encode(chunk) + b"\n\n"


# --- WebSocket -------------------------------------------------------------
#
# Hand-rolled because `http.server` has none and the alternative is a
# dependency for ~60 lines. It replaced SSE-out/POST-in, which was the wrong
# shape for a terminal: `fetch` promises no ordering and each POST landed on
# its own server thread, so fast typing arrived scrambled. A socket delivers in
# order by construction — that is the whole reason to prefer it here, latency
# is incidental.

_WS_GUID = b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
OP_TEXT, OP_BINARY, OP_CLOSE, OP_PING, OP_PONG = 0x1, 0x2, 0x8, 0x9, 0xA


def ws_accept(key: str) -> str:
    """The RFC 6455 handshake response: sha1 of the client key plus the magic
    GUID, base64'd. Proves to the browser that we understood the upgrade."""
    return base64.b64encode(hashlib.sha1(key.encode() + _WS_GUID).digest()).decode()


def ws_frame(payload: bytes, opcode: int = OP_BINARY) -> bytes:
    """One unfragmented server frame. Server-to-client frames are never
    masked."""
    head = bytes([0x80 | opcode])
    n = len(payload)
    if n < 126:
        head += bytes([n])
    elif n < 1 << 16:
        head += bytes([126]) + struct.pack(">H", n)
    else:
        head += bytes([127]) + struct.pack(">Q", n)
    return head + payload


def ws_read(rfile) -> tuple[int, bytes] | None:
    """Read one frame, unmasking it. None at end of stream.

    ponytail: assumes unfragmented frames. Browsers do not fragment the small
    messages a keyboard produces; if that ever changes, accumulate until FIN.
    """
    header = rfile.read(2)
    if len(header) < 2:
        return None
    opcode = header[0] & 0x0F
    masked = header[1] & 0x80
    length = header[1] & 0x7F
    if length == 126:
        length = struct.unpack(">H", rfile.read(2))[0]
    elif length == 127:
        length = struct.unpack(">Q", rfile.read(8))[0]
    mask = rfile.read(4) if masked else b""
    payload = rfile.read(length) if length else b""
    if masked:
        payload = bytes(byte ^ mask[i % 4] for i, byte in enumerate(payload))
    return opcode, payload


def demo() -> None:
    """Self-check: a session echoes what is written to it, replays scrollback to
    a late subscriber, and survives a reader disconnecting."""
    assert available(), "tmux required"
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        session = get("plexus-selfcheck", Path(tmp))
        try:
            queue, _ = session.subscribe()
            session.write(b"echo plexus-term-ok\n")
            seen = b""
            deadline = time.time() + 10
            while b"plexus-term-ok" not in seen and time.time() < deadline:
                try:
                    chunk = queue.get(timeout=1)
                except Empty:
                    continue
                if chunk is None:
                    break
                seen += chunk
            assert b"plexus-term-ok" in seen, seen[-400:]
            session.unsubscribe(queue)

            # a second viewer must receive the scrollback, not an empty pane
            _, backlog = session.subscribe()
            assert b"plexus-term-ok" in backlog, "scrollback not replayed"

            # Resize must actually reach tmux. It silently did nothing while the
            # client had no controlling terminal, so the pane was stuck at the
            # first size anyone requested and text wrapped at the wrong column.
            session.resize(130, 45)
            deadline = time.time() + 5
            seen = ""
            while time.time() < deadline:
                seen = _tmux("display-message", "-p", "-t", "plexus-selfcheck",
                             "#{pane_width}").stdout.strip()
                if seen == "130":
                    break
                time.sleep(0.2)
            assert seen == "130", f"resize ignored; pane is {seen} columns"

            # a job in a window: its output must land in the transcript and its
            # exit code must survive the window closing, or the server's reaper
            # reports every finished run as a failure
            work = Path(tmp)
            job = run_window("plexus-selfcheck", work, "job",
                             ["sh", "-c", "echo window-ran; exit 3"], {},
                             work / "t.log", work / "j.exit")
            assert job is not None, "tmux refused the window"
            deadline = time.time() + 15
            while job.poll() is None and time.time() < deadline:
                time.sleep(0.2)
            assert job.poll() == 3, f"exit code lost: {job.poll()}"
            assert "window-ran" in (work / "t.log").read_text(), "no transcript"
            names = [w["name"] for w in windows("plexus-selfcheck")]
            assert names, "session listed no windows"

            # replayed scrollback must not carry a capability query: answering
            # one late is how the shell ends up printing `1;2c0;276;0c`
            probe = b"before\x1b[c\x1b[>cmid\x1b[>qafter"
            assert _CAPABILITY_QUERY.sub(b"", probe) == b"beforemidafter", \
                _CAPABILITY_QUERY.sub(b"", probe)
            # …but a cursor-position report is content an app asked for
            assert _CAPABILITY_QUERY.sub(b"", b"\x1b[24;80R") == b"\x1b[24;80R"

            # The handshake GUID cannot be derived from anything, and getting a
            # character out of place fails only inside a real browser, with the
            # unhelpful message "Incorrect Sec-WebSocket-Accept header value".
            # Pinned against the published RFC 6455 vector.
            assert ws_accept("dGhlIHNhbXBsZSBub25jZQ==") == "s3pPLMBiTxaQ9kYGzzhZRbK+xOo=", \
                ws_accept("dGhlIHNhbXBsZSBub25jZQ==")
            # a masked client frame round-trips through the reader
            import io as _io
            masked = bytes([0x82, 0x82, 1, 2, 3, 4]) + bytes([ord("A") ^ 1, ord("B") ^ 2])
            assert ws_read(_io.BytesIO(masked)) == (OP_BINARY, b"AB")
            # and lengths cross the 126/65536 encoding boundaries correctly
            assert ws_frame(b"x" * 200)[:4] == bytes([0x82, 126, 0, 200])
            assert ws_frame(b"x" * 70000)[:2] == bytes([0x82, 127])

            # a drafting session: starts once, rejoins rather than stacking a
            # second one, and can actually be closed
            assert start("plexus-selfcheck-draft", work, "cat", "hello")
            assert exists("plexus-selfcheck-draft")
            assert not start("plexus-selfcheck-draft", work, "cat", "again"), \
                "a second click must rejoin, not spawn a duplicate"
            # the agent runs interactively: a one-shot invocation would have
            # exited by now, and the session must still be here to return to
            time.sleep(4)
            assert exists("plexus-selfcheck-draft"), "session did not survive its opening"
            assert kill("plexus-selfcheck-draft")
            assert not exists("plexus-selfcheck-draft"), "session outlived kill"
        finally:
            session.close()
            with _sessions_lock:
                _sessions.pop("plexus-selfcheck", None)
            subprocess.run(["tmux", "kill-session", "-t", "plexus-selfcheck"],
                           capture_output=True)
    print("term self-check ok")


if __name__ == "__main__":
    demo()
