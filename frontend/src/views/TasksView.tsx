import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { AlertTriangle, Check, ChevronDown, ChevronRight, FileText, Github, Lock, Play, Plus, X } from "lucide-react";
import { useState } from "react";
import { api } from "../api";
import { Discussion } from "../components/Discussion";
import { Panel, StatusBadge } from "../components/Status";
import type { Task } from "../types";

/** The work breakdown: every job, in the bucket that answers what you'd ask.
 *
 *  Four columns rather than six states, because a board is asked four
 *  questions — what shipped, what is moving, what is stuck and needs me, what
 *  is queued — and the underlying states are an implementation detail.
 *
 *  Order is the plan. A task runs only when everything it is blocked by has
 *  landed, and nothing new starts while something is in flight, so the queue
 *  below reads top to bottom as the sequence work will actually happen in. */

const BUCKETS = [
  ["active", "Active"], ["blocked", "Blocked"],
  ["planned", "Planned"], ["done", "Done"],
] as const;

function TaskCard({ task, isNext, root, busy, all }: {
  task: Task; isNext: boolean; root: string; busy: boolean; all: Task[];
}) {
  const client = useQueryClient();
  const [open, setOpen] = useState(false);
  const refresh = () => client.invalidateQueries({ queryKey: ["tasks", root] });
  const edit = useMutation({
    mutationFn: (fields: Record<string, unknown>) => api.post("/task", { root, id: task.id, ...fields }),
    onSuccess: refresh,
  });
  // Plan decomposes this task into features; run walks them. Both are scoped to
  // the task, so the project's other work is neither replanned nor touched.
  const plan = useMutation({
    mutationFn: () => api.post("/plan", { root, task: task.id }),
    onSuccess: refresh,
  });
  const start = useMutation({
    mutationFn: () => api.post("/run", { root, task: task.id }),
    onSuccess: refresh,
  });
  // A plan is written, then signed off, then walked. The sign-off had no button
  // anywhere, so every planned task answered run with "the current plan is not
  // approved" and there was no way in the UI to make it true.
  const approvePlan = useMutation({
    mutationFn: () => api.post("/approve", { root, task: task.id }),
    onSuccess: refresh,
  });
  const awaitingApproval = Boolean(task.plan_id) && !task.plan_approved;
  const design = [
    ["Types", task.design_types], ["Interfaces", task.design_interfaces],
    ["Call paths", task.design_call_paths],
  ] as const;
  return (
    <div className={`task-card ${isNext ? "is-next" : ""}`}>
      <button className="task-head" onClick={() => setOpen((v) => !v)} aria-expanded={open}>
        {open ? <ChevronDown size={13} /> : <ChevronRight size={13} />}
        <StatusBadge state={task.state} />
        <span className="task-title">{task.title}</span>
        {task.source_kind === "github" && <Github size={12} />}
        {task.pr > 0 && <span className="task-pr">#{task.pr}</span>}
      </button>
      <div className="task-meta">
        <code>{task.id}</code>
        {isNext && <em className="task-flag">next</em>}
        {task.waiting_on.length > 0 && (
          <span><Lock size={10} /> waiting on {task.waiting_on.length}</span>
        )}
        {task.needs_plan && <span>needs a plan before it can run</span>}
        {task.plan_id && <span>plan {task.plan_id}{task.plan_approved ? "" : " · awaiting approval"}</span>}
      </div>
      {task.error && (
        <div className="task-error">
          <AlertTriangle size={13} /> {task.error}
          <button className="button button-small" onClick={() => edit.mutate({ error: "", state: "open" })}>
            unblock
          </button>
        </div>
      )}
      {open && (
        <div className="task-detail">
          {task.body && <p className="task-body">{task.body}</p>}
          {/* Order is the plan, so the order has to be editable where you read
              it. A blocker used to be an id in a grey line with no way to see
              what it was or to change your mind about it. */}
          <div className="task-deps">
            <span>Blocked by</span>
            {task.blocked_by.length === 0 && <small>nothing — this can run when its turn comes</small>}
            <ul>
              {task.blocked_by.map((id) => {
                const blocker = all.find((t) => t.id === id);
                const done = !task.waiting_on.includes(id);
                return (
                  <li key={id} className={done ? "dep-done" : ""}>
                    {done ? <Check size={11} /> : <Lock size={11} />}
                    <strong>{blocker?.title || id}</strong>
                    <small>{done ? "landed" : blocker?.state || "unknown"}</small>
                    <button className="dep-drop" title={`Stop waiting on ${blocker?.title || id}`}
                      aria-label={`Remove dependency on ${blocker?.title || id}`}
                      onClick={() => edit.mutate({
                        blocked_by: task.blocked_by.filter((b) => b !== id),
                      })}>
                      <X size={11} />
                    </button>
                  </li>
                );
              })}
            </ul>
            <select
              className="panel-filter"
              value=""
              aria-label={`Add a blocker for ${task.title}`}
              onChange={(event) => event.target.value && edit.mutate({
                blocked_by: [...task.blocked_by, event.target.value],
              })}
            >
              <option value="">add a blocker…</option>
              {all.filter((t) => t.id !== task.id && !task.blocked_by.includes(t.id))
                  .map((t) => <option key={t.id} value={t.id}>{t.title}</option>)}
            </select>
          </div>
          {task.source_url && (
            <a href={task.source_url} target="_blank" rel="noreferrer">{task.source_url}</a>
          )}
          {/* Program design belongs to the task, not the project: the types and
              signatures a piece of work targets are decided per piece of work.
              Filled in here, they turn review into a check that the agreed
              shape was built. */}
          {design.map(([label, values]) => (
            <div className="task-design" key={label}>
              <span>{label}</span>
              {values.length
                ? <ul>{values.map((v) => <li key={v}>{v}</li>)}</ul>
                : <small>not designed yet</small>}
            </div>
          ))}
          <div className="task-actions">
            {task.needs_plan && (
              <button className="button button-small" disabled={plan.isPending}
                onClick={() => plan.mutate()}
                title="Break this task into features, planned inside the project overview">
                <FileText size={13} /> plan
              </button>
            )}
            {awaitingApproval && (
              <button className="button button-small button-primary"
                disabled={approvePlan.isPending}
                onClick={() => approvePlan.mutate()}
                title="Sign off on this task's plan — nothing runs until you do">
                <Check size={13} /> approve plan
              </button>
            )}
            {task.runnable && !awaitingApproval && (
              <button className="button button-small button-primary"
                disabled={busy || start.isPending}
                onClick={() => start.mutate()}
                title={busy ? "Something is already in flight — tasks run one at a time"
                            : task.plan_id ? "Walk this task's plan"
                            : "No plan needed — one feature is written from this task, then run"}>
                <Play size={13} /> run
              </button>
            )}
            <select
              className="panel-filter"
              value={task.state}
              onChange={(event) => edit.mutate({ state: event.target.value })}
              aria-label={`State for ${task.title}`}
            >
              {["open", "planning", "ready", "running", "blocked", "landed", "closed"].map((s) => (
                <option key={s} value={s}>{s}</option>
              ))}
            </select>
            {(edit.error || plan.error || start.error || approvePlan.error) && (
              <span className="inline-error">
                {(edit.error || plan.error || start.error || approvePlan.error)?.message}
              </span>
            )}
          </div>
        </div>
      )}
    </div>
  );
}

export function TasksView({ root }: { root: string }) {
  const client = useQueryClient();
  const board = useQuery({
    queryKey: ["tasks", root],
    queryFn: () => api.tasks(root),
    refetchInterval: 5_000,
  });
  const [title, setTitle] = useState("");
  const [body, setBody] = useState("");
  const [issue, setIssue] = useState("");
  const [blockedBy, setBlockedBy] = useState<string[]>([]);
  const [needsPlan, setNeedsPlan] = useState(true);
  const [adding, setAdding] = useState(false);

  const done = () => {
    setTitle(""); setBody(""); setIssue(""); setBlockedBy([]); setAdding(false);
    client.invalidateQueries({ queryKey: ["tasks", root] });
  };
  const add = useMutation({
    mutationFn: () => api.post("/task", { root, title, body, blocked_by: blockedBy, requires_plan: needsPlan }),
    onSuccess: done,
  });
  const fromIssue = useMutation({
    mutationFn: () => api.post("/task-from-issue", { root, url: issue }),
    onSuccess: done,
  });

  const data = board.data;
  const next = data?.next ?? "";

  return (
    <div className="stack">
      <Discussion root={root} view="tasks" label="Chat" title="Break down the work with the agent" />
      <Panel
        title={`Work breakdown · ${data?.tasks.length ?? 0}`}
        action={
          <button className="button button-small" onClick={() => setAdding((v) => !v)}>
            <Plus size={13} /> {adding ? "cancel" : "add task"}
          </button>
        }
      >
        {adding && (
          <div className="task-add">
            <form className="task-form" onSubmit={(e) => { e.preventDefault(); if (title.trim()) add.mutate(); }}>
              <label className="field">
                <span>Title</span>
                <input autoFocus value={title} onChange={(e) => setTitle(e.target.value)} placeholder="what this job is" />
              </label>
              <label className="field">
                <span>Detail</span>
                <textarea rows={2} value={body} onChange={(e) => setBody(e.target.value)} />
              </label>
              <fieldset className="field task-blockers">
                <legend>Blocked by <small>nothing, unless you say otherwise</small></legend>
                {!data?.tasks.length && <small>no other tasks yet</small>}
                {data?.tasks.filter((t) => t.state !== "landed" && t.state !== "closed").map((t) => (
                  <label className="check" key={t.id}>
                    <input type="checkbox" checked={blockedBy.includes(t.id)}
                      onChange={(e) => setBlockedBy(e.target.checked
                        ? [...blockedBy, t.id]
                        : blockedBy.filter((id) => id !== t.id))} />
                    <span>{t.title}</span>
                  </label>
                ))}
              </fieldset>
              <label className="check">
                <input type="checkbox" checked={needsPlan} onChange={(e) => setNeedsPlan(e.target.checked)} />
                <span>Needs its own plan before it can run</span>
              </label>
              <button className="button button-primary" disabled={!title.trim() || add.isPending}>
                <Plus size={14} /> Add
              </button>
            </form>
            <div className="task-issue">
              <label className="field">
                <span>…or import a GitHub issue</span>
                <input value={issue} onChange={(e) => setIssue(e.target.value)}
                  placeholder="https://github.com/owner/repo/issues/12" />
              </label>
              <button className="button" disabled={!issue.trim() || fromIssue.isPending}
                onClick={() => fromIssue.mutate()}>
                <Github size={14} /> Import
              </button>
            </div>
            {(add.error || fromIssue.error) && (
              <div className="inline-error">{(add.error || fromIssue.error)?.message}</div>
            )}
          </div>
        )}
        {data && !data.tasks.length && !adding && (
          <div className="panel-empty">
            No jobs yet. The overview says what the project is; a task is one piece of it.
          </div>
        )}
      </Panel>

      <div className="task-board">
        {BUCKETS.map(([key, label]) => (
          <section className={`task-column column-${key}`} key={key}>
            <h3>{label} <span>{data?.[key].length ?? 0}</span></h3>
            {data?.[key].map((task) => (
              <TaskCard key={task.id} task={task} isNext={task.id === next} root={root}
                busy={Boolean(data?.in_flight.length)} all={data?.tasks ?? []} />
            ))}
            {data && !data[key].length && <div className="column-empty">—</div>}
          </section>
        ))}
      </div>
    </div>
  );
}
