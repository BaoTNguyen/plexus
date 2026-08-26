import { FitAddon } from "@xterm/addon-fit";
import { Terminal } from "@xterm/xterm";
import { useEffect, useRef, useState } from "react";
import "@xterm/xterm/css/xterm.css";

/** The project's shell, live in the page.
 *
 *  Output arrives as base64 over SSE and input goes back one POST per
 *  keystroke. `http.server` speaks no WebSocket, and on loopback the round trip
 *  is well under a frame — writing a WebSocket codec to save it would be paying
 *  in maintenance for latency nobody can perceive.
 *
 *  Nothing here owns the session. It is a tmux session on the server, so this
 *  component attaching or leaving is not an event the shell notices. */
export function TerminalView({ root, sessionName, view = "shell", onHide }: {
  root: string;
  sessionName: string;
  /** which session of this project to attach to: the shell, or a conversation */
  view?: string;
  /** Detach the viewer. Deliberately not "end the session" — a button labelled
   *  close on a terminal panel reads as "hide this pane", so wiring it to a
   *  kill destroyed conversations people meant to collapse. */
  onHide?: () => void;
}) {
  const host = useRef<HTMLDivElement>(null);
  const [status, setStatus] = useState<
    "connecting" | "live" | "reconnecting" | "closed" | "error">("connecting");
  const [focused, setFocused] = useState(false);
  // Input failures used to be swallowed. A terminal that silently drops every
  // keystroke is indistinguishable from one that is merely unfocused, which is
  // exactly the wrong thing to be unable to tell apart.
  const [inputError, setInputError] = useState("");

  useEffect(() => {
    if (!host.current) return;
    const term = new Terminal({
      fontFamily: '"SFMono-Regular", "Cascadia Code", "Roboto Mono", Consolas, monospace',
      fontSize: 13,
      cursorBlink: true,
      // matches styles.css so the pane reads as part of the panel, not an
      // iframe someone dropped into it
      theme: {
        background: "#0b0f14", foreground: "#e8edf2", cursor: "#65a9ff",
        black: "#293440", red: "#fa665e", green: "#46c275", yellow: "#dca63a",
        blue: "#65a9ff", magenta: "#bd8cff", cyan: "#4cc9d1", white: "#e8edf2",
      },
      scrollback: 10_000,
      // the shell is the reason this pane exists, so it takes the keyboard the
      // moment it is on screen rather than waiting to be clicked
      screenReaderMode: false,
      allowProposedApi: true,
    });
    const fit = new FitAddon();
    term.loadAddon(fit);
    term.open(host.current);
    term.focus();
    fit.fit();

    // One WebSocket, both directions. Bytes in order, no base64, no request
    // per keypress. The previous transport (SSE out, POST in) could not keep
    // keystrokes in order, which is what made typing unusable.
    const url = new URL(`/api/term/ws`, window.location.href);
    url.protocol = url.protocol === "https:" ? "wss:" : "ws:";
    url.searchParams.set("root", root);
    url.searchParams.set("view", view);
    url.searchParams.set("cols", String(term.cols));
    url.searchParams.set("rows", String(term.rows));
    let socket: WebSocket | undefined;
    let alive = true;          // false once this effect is torn down
    let attempt = 0;           // reconnect backoff step
    let retry: number | undefined;

    const sendSize = () => {
      if (socket?.readyState === WebSocket.OPEN) {
        socket.send(JSON.stringify({ cols: term.cols, rows: term.rows }));
      }
    };

    const connect = () => {
      if (!alive) return;
      url.searchParams.set("cols", String(term.cols));
      url.searchParams.set("rows", String(term.rows));
      const sock = new WebSocket(url);
      socket = sock;
      sock.binaryType = "arraybuffer";
      // Every handler is gated on `alive` and on still being the current
      // socket. A superseded one still fires its close event, and without this
      // the dead one's "closed" lands after the live one's "live" and the panel
      // reports a working terminal as broken.
      sock.onopen = () => {
        if (!alive || socket !== sock) return;
        attempt = 0;
        setStatus("live");
        setInputError("");
        sendSize();
      };
      sock.onmessage = (event) => {
        if (alive && socket === sock) term.write(new Uint8Array(event.data));
      };
      sock.onclose = () => {
        if (!alive || socket !== sock) return;
        // The server restarts often — every rebuild — and a terminal that stays
        // dead until someone reloads the page reads as a broken terminal.
        // Backoff caps at 5s: this is loopback, so the only thing being waited
        // on is a process coming back up.
        const wait = Math.min(500 * 2 ** attempt, 5000);
        attempt += 1;
        setStatus("reconnecting");
        retry = window.setTimeout(() => {
          // tmux redraws the whole screen on attach and the server replays its
          // scrollback, so clear first or the reconnect prints a second copy of
          // everything under the first.
          term.reset();
          connect();
        }, wait);
      };
      sock.onerror = () => {
        if (alive && socket === sock) setInputError("connection to the terminal failed");
      };
    };
    connect();

    const encoder = new TextEncoder();
    const typed = term.onData((data) => {
      if (socket?.readyState === WebSocket.OPEN) socket.send(encoder.encode(data));
    });
    // mouse reports travel the same channel, so they keep their order relative
    // to keystrokes
    const clicked = term.onBinary((data) => {
      if (socket?.readyState === WebSocket.OPEN) socket.send(encoder.encode(data));
    });
    // resize is the one control message, sent as text so it can never be
    // confused with input

    // Focus the helper textarea directly. term.focus() delegates to it, but
    // going straight there survives the cases where xterm's own mouse handling
    // has already claimed the event.
    const refocus = () => (term.textarea ?? host.current)?.focus();
    host.current.addEventListener("mousedown", refocus);
    const onFocus = () => setFocused(true);
    const onBlur = () => setFocused(false);
    term.textarea?.addEventListener("focus", onFocus);
    term.textarea?.addEventListener("blur", onBlur);
    setFocused(document.activeElement === term.textarea);

    // one resize message per settled layout, not per pixel of a drag
    let pending: number | undefined;
    const observer = new ResizeObserver(() => {
      window.clearTimeout(pending);
      pending = window.setTimeout(() => {
        fit.fit();
        sendSize();
      }, 120);
    });
    observer.observe(host.current);

    const node = host.current;
    return () => {
      window.clearTimeout(pending);
      observer.disconnect();
      node.removeEventListener("mousedown", refocus);
      term.textarea?.removeEventListener("focus", onFocus);
      term.textarea?.removeEventListener("blur", onBlur);
      alive = false;
      window.clearTimeout(retry);
      typed.dispose();
      clicked.dispose();
      if (socket) {
        socket.onopen = socket.onmessage = socket.onclose = socket.onerror = null;
        socket.close();
      }
      term.dispose();
    };
  }, [root, view]);

  return (
    <div className="terminal-panel">
      <div className="terminal-bar">
        <span className={`terminal-dot dot-${status}`} />
        <code>{sessionName}</code>
        <span className="terminal-status">{status}</span>
        <span className={`terminal-focus ${focused ? "has-focus" : ""}`}>
          {focused ? "keyboard connected" : "click to type"}
        </span>
        {inputError && <span className="terminal-input-error">{inputError}</span>}
        {/* the attach command is genuinely useful if you live in a terminal,
            but it is a detail, not an instruction — nothing here requires
            knowing tmux */}
        <span className="terminal-hint">
          also yours from a real shell: <code>tmux attach -t {sessionName}</code>
        </span>
        {onHide && (
          <button className="button button-small" onClick={onHide}
            title="Hide this pane. The session keeps running — reopen any time.">
            hide
          </button>
        )}
      </div>
      <div className="terminal-host" ref={host} tabIndex={-1} onClick={() => host.current?.querySelector("textarea")?.focus()} />
    </div>
  );
}
