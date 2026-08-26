import { useQuery, useQueryClient } from "@tanstack/react-query";
import { FileClock, SquareTerminal } from "lucide-react";
import { useState } from "react";
import { api } from "../api";
import { Panel } from "../components/Status";
import { TerminalView } from "./TerminalView";

/** Build: the 20% of the ratio, in one place.
 *
 *  Coding happens in a terminal whether you are driving or an agent is, so
 *  there is nothing to separate. Runs land in windows of this project's tmux
 *  session, and this switches between them and your own shell. A window that
 *  has closed leaves a transcript, which is usually the thing you want to read
 *  — a run stops being live at the exact moment you go looking for it. */
export function BuildView({ root, sessionName }: { root: string; sessionName: string }) {
  const client = useQueryClient();
  const [transcript, setTranscript] = useState<string | null>(null);
  const listing = useQuery({
    queryKey: ["term-windows", root],
    queryFn: () => api.termWindows(root),
    refetchInterval: 3_000,
  });
  const text = useQuery({
    queryKey: ["transcript", root, transcript],
    queryFn: () => api.termTranscript(root, transcript!),
    enabled: Boolean(transcript),
  });

  const select = async (id: string) => {
    setTranscript(null);
    await api.post("/term/select", { root, window: id });
    client.invalidateQueries({ queryKey: ["term-windows", root] });
  };

  return (
    <div className="build-workspace">
      <div className="window-bar" role="tablist" aria-label="Windows and past runs">
        {listing.data?.windows.map((w) => (
          <button
            key={w.id}
            role="tab"
            aria-selected={!transcript && w.active}
            className={`window-chip ${!transcript && w.active ? "active" : ""}`}
            onClick={() => select(w.id)}
          >
            <SquareTerminal size={12} /> {w.name}
          </button>
        ))}
        {listing.data?.transcripts.map((t) => (
          <button
            key={t.file}
            role="tab"
            aria-selected={transcript === t.file}
            className={`window-chip past ${transcript === t.file ? "active" : ""}`}
            onClick={() => setTranscript(t.file)}
            title={`finished ${t.finished}`}
          >
            <FileClock size={12} /> {t.name}
          </button>
        ))}
        {!listing.data?.windows.length && !listing.data?.transcripts.length && (
          <span className="window-empty">No shell yet — open one below.</span>
        )}
      </div>

      {transcript ? (
        <Panel
          title={`Transcript · ${transcript}`}
          action={<button className="button button-small" onClick={() => setTranscript(null)}>back to live</button>}
        >
          {/* raw recording, escape codes and all: it is what the agent's CLI
              actually printed, not a cleaned-up retelling of it */}
          <pre className="transcript-output">{text.data?.text ?? "loading…"}</pre>
        </Panel>
      ) : (
        <TerminalView root={root} sessionName={sessionName} />
      )}
    </div>
  );
}
