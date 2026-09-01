import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Bot, Eye, EyeOff } from "lucide-react";
import { api } from "../api";
import { TerminalView } from "../views/TerminalView";

/** One conversation per tab, rendered in the tab that started it.
 *
 *  Not one button per field: the fields are not independent, and a model that
 *  can only see one keeps re-proposing what the others settled. The agent opens
 *  with everything the tab currently holds, so the first turn reads the state
 *  instead of asking you to paste it back.
 *
 *  It renders here rather than sending you to the runs tab, because being told
 *  to go somewhere else to watch the thing you just started is not a workflow.
 *  Hiding the pane and leaving the tab both leave the session running. Only
 *  "end conversation" kills it — a button labelled close on a terminal reads
 *  as "collapse this", and wiring that to a kill destroyed conversations
 *  people meant to put away for a minute. */
export function Discussion({ root, view, label }: {
  root: string; view: "overview" | "tasks"; label: string;
}) {
  const client = useQueryClient();
  const [hidden, setHidden] = useState(false);
  const listing = useQuery({
    queryKey: ["term-sessions", root],
    queryFn: () => api.termSessions(root),
    refetchInterval: 4_000,
  });
  const open = listing.data?.sessions.find((s) => s.view === view);

  const refresh = () => client.invalidateQueries({ queryKey: ["term-sessions", root] });
  const start = useMutation({
    mutationFn: () => api.post("/discuss", { root, view }),
    onSuccess: refresh,
  });
  // The only thing that ends a conversation. Hiding the pane does not, and
  // neither does leaving the tab — a chat lasts until you say so.
  const end = useMutation({
    mutationFn: () => api.post("/term/close", { root, view }),
    onSuccess: () => { setHidden(false); refresh(); },
  });

  return (
    <div className="discussion">
      <div className="discussion-bar">
        <button
          className="button button-primary"
          onClick={() => { setHidden(false); if (!open) start.mutate(); }}
          disabled={start.isPending}
          title={open ? "Already running — this brings it back into view" : label}
        >
          <Bot size={14} /> {open ? "conversation running" : label}
        </button>
        {open && (
          <>
            <button className="button" onClick={() => setHidden((v) => !v)}>
              {hidden ? <><Eye size={13} /> show</> : <><EyeOff size={13} /> hide</>}
            </button>
            <button className="button button-danger" onClick={() => end.mutate()}
              disabled={end.isPending}
              title="Ends the session for good. Everything else leaves it running.">
              end conversation
            </button>
          </>
        )}
        {(start.error || end.error) && (
          <span className="inline-error">{(start.error || end.error)?.message}</span>
        )}
      </div>
      {open && !hidden && (
        <TerminalView
          root={root}
          view={view}
          sessionName={open.name}
          onHide={() => setHidden(true)}
        />
      )}
      {open && hidden && (
        <div className="discussion-hidden">
          Conversation still running in <code>{open.name}</code> — press show to bring it back.
        </div>
      )}
    </div>
  );
}
