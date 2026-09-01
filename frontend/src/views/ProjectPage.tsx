import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useParams } from "@tanstack/react-router";
import { Pause, Play } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { useVirtualizer } from "@tanstack/react-virtual";
import { api } from "../api";
import { Panel, StatusBadge, TierBadge } from "../components/Status";
import type { EventRow, Goal } from "../types";
import { OverviewView } from "./OverviewView";
import { RunsView } from "./RunsView";
import { TasksView } from "./TasksView";
import { ValidationView } from "./ValidationView";

// Four tabs, one per thing you manage. `overview` is the standing charter and
// never closes; `tasks` is the work broken out of it; `runs` is that work
// happening; `validation` is what says it is done. Features, plans, escalations
// and the ledger were tabs describing the implementation, so they moved inside
// the phase they belong to.
const tabs = ["overview", "tasks", "runs", "validation"] as const;

function Features({ root }: { root: string }) {
  const detail = useQuery({ queryKey: ["goal", root], queryFn: () => api.goal(root), refetchInterval: 5_000 });
  return (
    <Panel title="Work item board">
      {detail.data?.features.map((feature) => (
        <div className="feature-row" key={feature.id}>
          <span>{feature.id}</span>
          <div>
            <strong>{feature.title}</strong>
            <small>{feature.depends_on.length ? `after ${feature.depends_on.join(", ")} · ` : ""}priority {feature.priority} · {feature.acceptance}</small>
          </div>
          <span>a{feature.attempt}</span>
          <StatusBadge state={feature.state} />
        </div>
      ))}
      {detail.data && !detail.data.features.length && <div className="panel-empty">No plan yet.</div>}
    </Panel>
  );
}

function Episodes({ root, projectId }: { root: string; projectId: string }) {
  const query = useQuery({
    queryKey: ["episodes", root],
    queryFn: () => api.episodes(root),
    refetchInterval: 5_000,
  });
  const live = query.data?.filter((episode) => episode.state === "running") || [];
  const past = query.data?.filter((episode) => episode.state !== "running") || [];
  const rows = (title: string, episodes: typeof live) => (
    <Panel title={`${title} · ${episodes.length}`}>
      {episodes.map((episode) => (
        <Link
          className="episode-row episode-row-wide"
          to="/p/$projectId/ep/$episodeId"
          params={{ projectId, episodeId: episode.episode_id }}
          key={episode.episode_id}
        >
          <StatusBadge state={episode.state} />
          <strong>{episode.feature_id || episode.episode_id}</strong>
          <span>a{episode.attempt}</span>
          <span>{episode.verify_rounds} verify</span>
          <TierBadge tier={episode.tier} agent={episode.agent} />
          <span>${episode.cost_usd.toFixed(2)}</span>
          <time>{episode.started.slice(0, 19).replace("T", " ")}</time>
        </Link>
      ))}
      {!episodes.length && <div className="panel-empty compact">None.</div>}
    </Panel>
  );
  return <div className="stack">{rows("Live", live)}{rows("Past", past)}</div>;
}

function LiveLog({ root }: { root: string }) {
  const [events, setEvents] = useState<EventRow[]>([]);
  const [cursor, setCursor] = useState("1970-01-01T00:00:00+00:00");
  const [paused, setPaused] = useState(false);
  const [error, setError] = useState("");
  const scrollRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    setEvents([]);
    setCursor("1970-01-01T00:00:00+00:00");
  }, [root]);
  useEffect(() => {
    if (paused) return;
    let alive = true;
    const poll = async () => {
      try {
        const response = await fetch(
          `/api/live?root=${encodeURIComponent(root)}&since=${encodeURIComponent(cursor)}&limit=2000`,
        );
        const body = await response.json();
        if (!response.ok) throw new Error(body.error || "live stream failed");
        if (alive && body.events.length) {
          setEvents((current) => [...current, ...body.events].slice(-5000));
          setCursor(body.cursor);
        }
        if (alive) setError("");
      } catch (caught) {
        if (alive) setError((caught as Error).message);
      }
    };
    poll();
    const timer = window.setInterval(poll, 2_000);
    return () => { alive = false; window.clearInterval(timer); };
  }, [cursor, paused, root]);
  const virtualizer = useVirtualizer({
    count: events.length,
    getScrollElement: () => scrollRef.current,
    estimateSize: () => 29,
    overscan: 12,
  });
  useEffect(() => {
    if (!paused && events.length) virtualizer.scrollToIndex(events.length - 1);
  }, [events.length, paused, virtualizer]);
  return (
    <Panel
      title={`Live tail · ${events.length.toLocaleString()}`}
      action={
        <button className="button button-small" onClick={() => setPaused((value) => !value)}>
          {paused ? <Play size={13} /> : <Pause size={13} />} {paused ? "resume" : "pause"}
        </button>
      }
    >
      {error && <div className="inline-error">{error}</div>}
      <div className="virtual-log" ref={scrollRef} role="log" aria-live={paused ? "off" : "polite"}>
        <div style={{ height: virtualizer.getTotalSize(), position: "relative" }}>
          {virtualizer.getVirtualItems().map((item) => {
            const event = events[item.index];
            return (
              <div
                className="event-row"
                key={`${event.ts}-${item.index}`}
                style={{ transform: `translateY(${item.start}px)` }}
              >
                <time>{event.ts.slice(11, 19)}</time>
                <span className={`source source-${event.source}`}>{event.source}</span>
                <strong>{event.kind}</strong>
                <span>{event.detail}</span>
              </div>
            );
          })}
        </div>
      </div>
    </Panel>
  );
}

function PlanView({ root }: { root: string }) {
  const detail = useQuery({ queryKey: ["goal", root], queryFn: () => api.goal(root) });
  return (
    <Panel title={detail.data?.approved ? "Approved plan" : "Draft plan"}>
      <pre className="plan-output">
        {detail.data?.features.map((feature) =>
          `${feature.id}  ${feature.title}\n    priority ${feature.priority}`
          + `${feature.depends_on.length ? ` · after ${feature.depends_on.join(", ")}` : ""}`
          + `\n    automated: ${feature.acceptance}`
          + `${feature.manual_checks.length ? `\n    manual: ${feature.manual_checks.join("; ")}` : ""}`,
        ).join("\n\n") || "No plan yet."}
      </pre>
    </Panel>
  );
}

function Activity({ root }: { root: string }) {
  const detail = useQuery({ queryKey: ["goal", root], queryFn: () => api.goal(root), refetchInterval: 5_000 });
  return (
    <div className="stack">
      <Panel title="Ledger activity">
        {detail.data?.activity.map((row, index) => (
          <div className="activity-row" key={`${row.ts}-${index}`}>
            <time>{row.ts.slice(0, 19).replace("T", " ")}</time>
            <strong>{row.kind}</strong><span>{row.feature_id}</span><span>{row.reason}</span>
          </div>
        ))}
      </Panel>
      <Panel title="Insights"><pre className="health-output">{detail.data?.insights.join("\n")}</pre></Panel>
    </div>
  );
}

function Blocks({ root }: { root: string }) {
  const client = useQueryClient();
  const detail = useQuery({ queryKey: ["goal", root], queryFn: () => api.goal(root), refetchInterval: 5_000 });
  const [answers, setAnswers] = useState<Record<string, string>>({});
  const resolve = useMutation({
    mutationFn: ({ feature, answer }: { feature: string; answer: string }) =>
      api.post("/resolve", { root, feature, answer }),
    onSuccess: () => {
      client.invalidateQueries({ queryKey: ["goal", root] });
      client.invalidateQueries({ queryKey: ["dashboard"] });
    },
  });
  return (
    <Panel title={`Open escalations · ${detail.data?.escalations.length || 0}`}>
      {detail.data?.escalations.map((item) => (
        <div className="block-card" key={item.feature_id}>
          <div><strong>{item.feature_id}</strong><StatusBadge state="blocked" /></div>
          <p>{item.reason_class} · {item.reason}</p>
          <textarea
            value={answers[item.feature_id] || ""}
            onChange={(event) => setAnswers({ ...answers, [item.feature_id]: event.target.value })}
            placeholder="answer or decision…"
          />
          <button
            className="button button-primary"
            disabled={!answers[item.feature_id] || resolve.isPending}
            onClick={() => resolve.mutate({ feature: item.feature_id, answer: answers[item.feature_id] })}
          >
            Resolve
          </button>
        </div>
      ))}
      {detail.data && !detail.data.escalations.length && <div className="panel-empty">No open escalations.</div>}
    </Panel>
  );
}

export function ProjectPage() {
  const { projectId, tab } = useParams({ from: "/p/$projectId/$tab" });
  const goals = useQuery({ queryKey: ["goals"], queryFn: api.goals });
  const goal = goals.data?.find((item: Goal) => item.project_id === projectId);
  const detail = useQuery({
    queryKey: ["goal", goal?.root],
    queryFn: () => api.goal(goal!.root),
    enabled: Boolean(goal),
    refetchInterval: 5_000,
  });
  if (goals.isLoading) return <div className="loading-grid" />;
  if (!goal) return <div className="empty">Unknown project.</div>;
  // no tmux on this host means no persistent shell, so the tab is not offered.
  // Absent is not the same as false: while /api/fleet is still in flight the
  // tab stays hidden but a deep link to it must survive, or bookmarking the
  // terminal silently lands you on `goal`.
  const activeTab = tabs.includes(tab as typeof tabs[number]) ? tab : "overview";
  return (
    <div className="page">
      <div className="project-heading">
        <div>
          <p className="eyebrow">{goal.root}</p>
          <h1>{goal.name}</h1>
          <span>{goal.goal_id} · {(detail.data?.lifecycle.state || goal.goal_state).replaceAll("_", " ")}</span>
        </div>
        <StatusBadge state={detail.data?.lifecycle.state || goal.goal_state} />
      </div>
      <nav className="tabs">
        {tabs.map((item) => (
          <Link
            to="/p/$projectId/$tab"
            params={{ projectId, tab: item }}
            className={activeTab === item ? "active" : ""}
            key={item}
          >
            {item}{item === "tasks" && detail.data?.escalations.length ? ` ${detail.data.escalations.length}` : ""}
          </Link>
        ))}
      </nav>
      {activeTab === "overview" && <OverviewView root={goal.root} />}
      {activeTab === "tasks" && (
        <div className="stack">
          <TasksView root={goal.root} />
          {/* answering a block is how a blocked task becomes runnable again,
              so the form lives beside the board it unblocks */}
          <Blocks root={goal.root} />
          <PlanView root={goal.root} />
          <Features root={goal.root} />
        </div>
      )}
      {activeTab === "runs" && (
        <RunsView
          root={goal.root}
          projectId={projectId}
          sessionName={goal.term_session}
          log={<div className="build-stack"><LiveLog root={goal.root} /><Activity root={goal.root} /></div>}
        />
      )}
      {activeTab === "validation" && (
        <div className="stack">
          <ValidationView root={goal.root} />
          <Episodes root={goal.root} projectId={projectId} />
        </div>
      )}
    </div>
  );
}
