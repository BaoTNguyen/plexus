import { FitAddon } from "@xterm/addon-fit";
import { Terminal } from "@xterm/xterm";
import { useQuery } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";
import { api } from "../api";
import "@xterm/xterm/css/xterm.css";

/** A finished run or conversation, read three ways.
 *
 *  The file is a `script(1)` typescript: every byte the agent's CLI wrote,
 *  cursor moves and repaints included. Three readings of it, none of which
 *  changes a byte on disk:
 *
 *  - **log** — the server replays the recording through a screen model and
 *    hands back the scrollback a line at a time, with a time of day on each.
 *    This is the default, because "what happened, in order" is the question
 *    people actually bring to a log.
 *  - **terminal** — the same bytes, redrawn by the same emulator that draws
 *    the live pane, at the width the recording was made at. Anything else
 *    lands every absolute cursor move in the wrong column, which is what turned
 *    the replay into interleaved fragments.
 *  - **raw** — escapes stripped, nothing else interpreted. The escape hatch
 *    for when the other two disagree with you.
 */

type Mode = "log" | "terminal" | "raw";

/** width the recording was made at, from the header `script` writes */
function recordedCols(text: string): number {
  const found = /COLUMNS="(\d+)"/.exec(text.slice(0, 4096));
  const cols = found ? Number(found[1]) : 80;
  return cols >= 20 && cols <= 500 ? cols : 80;
}

function strip(text: string): string {
  return text
    .replace(/\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)/g, "")
    .replace(/\x1b[[\]][0-9;?]*[ -/]*[@-~]/g, "")
    .replace(/\x1b[()][A-Za-z0-9]/g, "")
    .replace(/\x1b[=>]/g, "")
    .replace(/\r(?!\n)/g, "\n")
    .replace(/[\x00-\x08\x0b\x0c\x0e-\x1f]/g, "");
}

function Replay({ text }: { text: string }) {
  const host = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!host.current) return;
    const term = new Terminal({
      fontFamily: '"SFMono-Regular", "Cascadia Code", "Roboto Mono", Consolas, monospace',
      fontSize: 13,
      disableStdin: true,
      cursorBlink: false,
      // the recorded width, never the panel's: a recording is a grid, and
      // replaying it into a different one scrambles it
      cols: recordedCols(text),
      rows: 24,
      theme: {
        background: "#0b0f14", foreground: "#e8edf2", cursor: "#0b0f14",
        black: "#293440", red: "#fa665e", green: "#46c275", yellow: "#dca63a",
        blue: "#65a9ff", magenta: "#bd8cff", cyan: "#4cc9d1", white: "#e8edf2",
      },
      scrollback: 100_000,
    });
    const fit = new FitAddon();
    term.loadAddon(fit);
    term.open(host.current);
    // fit chooses rows only; the column count stays as recorded
    term.resize(recordedCols(text), Math.max(10, term.rows));
    term.write(text);
    return () => term.dispose();
  }, [text]);
  return <div className="transcript-term" ref={host} />;
}

function LogLines({ root, name }: { root: string; name: string }) {
  const query = useQuery({
    queryKey: ["transcript-lines", root, name],
    queryFn: () => api.termTranscriptLines(root, name),
  });
  const [filter, setFilter] = useState("");
  if (query.isLoading) return <div className="loading-grid" aria-label="Replaying recording" />;
  if (query.error) return <div className="inline-error">{(query.error as Error).message}</div>;
  const rows = (query.data?.lines ?? []).filter(
    (line) => !filter || line.text.toLowerCase().includes(filter.toLowerCase()));
  const stamped = rows.some((line) => line.ts);
  return (
    <div className="transcript-log">
      <div className="transcript-filter">
        <input value={filter} onChange={(event) => setFilter(event.target.value)}
          placeholder="filter lines…" aria-label="Filter log lines" />
        <small>
          {rows.length.toLocaleString()} lines
          {stamped ? "" : " · recorded before timing was kept, so no clock on these"}
        </small>
      </div>
      <ol className="transcript-lines">
        {rows.map((line, index) => (
          <li key={index}>
            <time>{line.ts}</time>
            <span>{line.text || " "}</span>
          </li>
        ))}
        {!rows.length && <li className="panel-empty compact">Nothing matches.</li>}
      </ol>
    </div>
  );
}

export function TranscriptView({ root, name, text }: {
  root: string; name: string; text: string;
}) {
  const [mode, setMode] = useState<Mode>("log");
  const modes: Array<[Mode, string]> = [
    ["log", "log"], ["terminal", "terminal"], ["raw", "raw"],
  ];
  return (
    <div className="transcript">
      <div className="transcript-bar" role="tablist" aria-label="How to read this recording">
        {modes.map(([key, label]) => (
          <button key={key} role="tab" aria-selected={mode === key}
            className={`button button-small ${mode === key ? "button-primary" : ""}`}
            onClick={() => setMode(key)}>
            {label}
          </button>
        ))}
        <small>
          {mode === "log" ? "replayed through a screen, one line per line"
            : mode === "terminal" ? "redrawn at the width it was recorded at"
            : "escape codes stripped, nothing else interpreted"}
        </small>
      </div>
      {mode === "log" && <LogLines root={root} name={name} />}
      {mode === "terminal" && <Replay text={text} />}
      {mode === "raw" && <pre className="transcript-output">{strip(text)}</pre>}
    </div>
  );
}
