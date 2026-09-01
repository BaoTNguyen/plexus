import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { Link } from "@tanstack/react-router";
import { AlertTriangle, ArrowRight, MessageSquareText, Radio, Workflow } from "lucide-react";
import { api } from "../api";
import { Panel, StatusBadge, TierBadge } from "../components/Status";
import type { Dashboard } from "../types";

const money = (value: number) => `$${value.toFixed(2)}`;
const count = (value: number) => new Intl.NumberFormat("en", { notation: "compact" }).format(value);
const elapsed = (started: string) => {
  const seconds = Math.max(0, (Date.now() - new Date(started).getTime()) / 1000);
  if (seconds < 60) return `${Math.floor(seconds)}s`;
  return `${Math.floor(seconds / 60)}m ${Math.floor(seconds % 60)}s`;
};

const GAP_LABELS: Record<string, string> = {
  unmeasured: "no usage reported",
  unattributed: "no provider",
  unpriced: "no rate card",
};

/** Turns that produced no cost figure, and why. Without this the panel shows a
 *  total with no indication of what it excluded, so a provider that never
 *  reports usage is indistinguishable from one that costs nothing. Renders
 *  nothing when every turn priced — an empty state here is genuinely good news. */
function CostGaps({ gaps }: { gaps: Dashboard["cost"]["gaps"] }) {
  const entries = Object.entries(gaps ?? {}).filter(([, n]) => n > 0);
  if (!entries.length) return null;
  const total = entries.reduce((sum, [, n]) => sum + n, 0);
  return (
    <div className="token-total cost-gaps">
      {total} turn{total === 1 ? "" : "s"} not priced ·{" "}
      {entries.map(([reason, n]) => `${n} ${GAP_LABELS[reason] ?? reason}`).join(" · ")}
    </div>
  );
}

/** Hours, in the order they appear in the picker. 0 is all time — the API reads
 *  it as "no lower bound" rather than a sentinel date. */
const WINDOWS: [number, string][] = [
  [1, "last hour"], [12, "last 12 hours"], [24, "last 24 hours"], [48, "last 48 hours"],
  [24 * 7, "last 7 days"], [24 * 30, "last month"], [24 * 365, "last year"], [0, "all time"],
];

export function DashboardPage() {
  const [windowH, setWindowH] = useState(
    () => Number(localStorage.getItem("plexus.window") ?? 24),
  );
  const query = useQuery({
    queryKey: ["dashboard", windowH],
    queryFn: () => api.dashboard(windowH),
    refetchInterval: 5_000,
    // the previous window's numbers stay up while the next fetch lands, so
    // changing the range never blanks the page back to the loading grid
    placeholderData: (previous) => previous,
  });
  const data = query.data;
  const [costProvider, setCostProvider] = useState("all");
  if (!data) return <div className="loading-grid" aria-label="Loading dashboard" />;
  // "all" sums the per-provider rows rather than reading data.cost.total, so the
  // headline and every filtered view come from one source and cannot disagree
  const rows = data.cost.by_provider;
  const picked = rows.filter((p) => costProvider === "all" || p.provider === costProvider);
  const costRow = picked.reduce(
    (acc, p) => ({
      cost: acc.cost + p.cost, equivalent: acc.equivalent + p.equivalent,
      tokens: acc.tokens + p.tokens,
      tokens_in: acc.tokens_in + p.tokens_in, tokens_out: acc.tokens_out + p.tokens_out,
      cache_tokens: acc.cache_tokens + p.cache_tokens,
    }),
    { cost: 0, equivalent: 0, tokens: 0, tokens_in: 0, tokens_out: 0, cache_tokens: 0 },
  );
  const live = data.recent_episodes.filter((episode) => episode.state === "running");
  return (
    <div className="page dashboard">
      <div className="page-heading">
        <div>
          <p className="eyebrow">Fleet overview</p>
          <h1>Control plane</h1>
        </div>
        <div className="page-window">
          {query.isFetching && <span className="updated">updating…</span>}
          <select
            className="panel-filter"
            value={windowH}
            onChange={(event) => {
              const next = Number(event.target.value);
              setWindowH(next);
              localStorage.setItem("plexus.window", String(next));
            }}
            aria-label="Time window for usage, cost and activity"
          >
            {WINDOWS.map(([hours, label]) => (
              <option key={hours} value={hours}>{label}</option>
            ))}
          </select>
        </div>
      </div>

      <Panel
        title={`Needs you · ${data.alerts.length}`}
        className={data.alerts.length ? "needs-you" : ""}
        action={<Link to="/alerts">all alerts <ArrowRight size={13} /></Link>}
      >
        {data.alerts.length ? (
          <div className="alert-list">
            {data.alerts.slice(0, 5).map((alert, index) => (
              <Link
                key={`${alert.project_id}-${alert.feature_id}-${index}`}
                to="/p/$projectId/$tab"
                params={{ projectId: alert.project_id, tab: "blocks" }}
                className="alert-row"
              >
                <AlertTriangle size={15} />
                <strong>{alert.goal_id}</strong>
                <span>{alert.feature_id}</span>
                <span className="alert-reason">{alert.reason}</span>
                <StatusBadge state={alert.severity} />
              </Link>
            ))}
          </div>
        ) : (
          <div className="panel-empty compact">Nothing needs a decision.</div>
        )}
      </Panel>

      <div className="metric-grid">
        <div className="metric">
          <MessageSquareText size={15} />
          <span>observed turns</span>
          <strong>{data.activity.turns}</strong>
          <small>{data.activity.active_5m} events in 5m</small>
        </div>
        <div className="metric">
          <Radio size={15} />
          <span>running</span>
          <strong>{data.runs.running}</strong>
          <small>{data.runs.projects} projects</small>
        </div>
        <div className="metric">
          <Workflow size={15} />
          <span>landed</span>
          <strong>{data.runs.landed}</strong>
          <small>{data.runs.landed_7d} in 7d</small>
        </div>
        <div className="metric metric-danger">
          <AlertTriangle size={15} />
          <span>blocked</span>
          <strong>{data.runs.blocked}</strong>
          <small>{data.runs.stalled} stalled</small>
        </div>
      </div>

      <div className="dashboard-grid">
        <Panel
          title={`Observed activity · ${count(data.activity.events)} events`}
          action={data.activity.last_event ? <span>last {elapsed(data.activity.last_event)} ago</span> : undefined}
        >
          <div className="activity-summary">
            <span><strong>{data.activity.turns}</strong> user turns</span>
            <span><strong>{data.activity.responses}</strong> responses</span>
            <span><strong>{data.activity.retrievals}</strong> retrievals</span>
          </div>
          <div className="spend-list">
            {data.activity.by_source.map((source) => (
              <div className="spend-row" key={source.source}>
                <span>{source.source}</span>
                <div><i style={{ width: `${data.activity.events ? source.events / data.activity.events * 100 : 0}%` }} /></div>
                <strong>{source.events}</strong>
              </div>
            ))}
          </div>
          <div className="token-total">
            {/* cache traffic listed on its own: it is usually most of what a
                turn sends and a tenth of what input costs, so folding it into
                "in" would misstate both the volume and the price */}
            {data.activity.metered_turns
              ? `${count(data.activity.tokens_in)} in / ${count(data.activity.tokens_out)} out`
                + (data.activity.cache_read || data.activity.cache_write
                  ? ` · ${count(data.activity.cache_read)} cached / ${count(data.activity.cache_write)} written`
                  : "")
              : "Subscription turns observed · exact token usage unavailable"}
          </div>
        </Panel>

        {/* Reading only. The rates behind these numbers are configuration and
            live in the settings dialog, where a form belongs. */}
        <Panel
          title="Cost"
          action={
            /* Provider is a filter, not a set of rows: with one plan doing the
               work and another idle, a stacked list buries the total. Pick a
               plan to scope both headline figures to it. */
            <select
              className="panel-filter"
              value={costProvider}
              onChange={(event) => setCostProvider(event.target.value)}
              aria-label="Filter cost by provider"
            >
              <option value="all">all providers</option>
              {data.cost.by_provider.map((p) => (
                <option key={p.provider} value={p.provider}>{p.provider}</option>
              ))}
            </select>
          }
        >
          {/* API equivalent leads, not the subscription: a plan is a fixed
              cost you already know, while this scales with what you actually
              used. It is a counterfactual — nothing bills you this — so the
              label says so rather than implying money left the account. */}
          <div className="cost-headline">
            <div>
              <span>API equivalent</span>
              <strong>{money(costRow.equivalent)}</strong>
            </div>
            <div>
              <span>total tokens</span>
              <strong>{count(costRow.tokens)}</strong>
              <small>
                {count(costRow.tokens_in)} in · {count(costRow.tokens_out)} out
                {costRow.cache_tokens > 0 && <> · {count(costRow.cache_tokens)} cached</>}
              </small>
            </div>
          </div>
          <div className="cost-breakdown">
            <span><strong>{money(data.cost.metered_api)}</strong> metered API</span>
            <span><strong>{data.cost.local.turns}</strong> local turns · {count(data.cost.local.tokens)} tokens</span>
          </div>
        </Panel>

        <Panel title="Live now">
          {live.length ? live.map((episode) => (
            <Link
              className="episode-row"
              to="/p/$projectId/ep/$episodeId"
              params={{ projectId: episode.project_id!, episodeId: episode.episode_id }}
              key={`${episode.project_id}-${episode.episode_id}`}
            >
              <span className="pulse" />
              <strong>{episode.goal_id}</strong>
              <span>{episode.feature_id} · a{episode.attempt}</span>
              <TierBadge tier={episode.tier} agent={episode.agent} />
              <time>{elapsed(episode.started)}</time>
              <ArrowRight size={14} />
            </Link>
          )) : <div className="panel-empty">Nothing is running.</div>}
        </Panel>

        <Panel title="Fleet cost by project" action={<span>marginal spend</span>}>
          <div className="spend-list">
            {/* Marginal money only — spend that exists because a run happened.
                Subscription share is deliberately absent: it is owed whether or
                not the fleet did anything, so ranking projects by it says
                nothing about what they cost. */}
            {data.cost.by_project.slice(0, 8).map((project) => {
              const peak = Math.max(...data.cost.by_project.map((p) => p.marginal_usd), 0);
              const width = peak ? (project.marginal_usd / peak) * 100 : 0;
              const row = (
                <>
                  <span>{project.name}</span>
                  <div><i style={{ width: `${width}%` }} /></div>
                  <strong>{money(project.marginal_usd)}</strong>
                </>
              );
              if (project.unassigned) {
                return <div className="spend-row" key="unassigned">{row}</div>;
              }
              return (
                <Link
                  to="/p/$projectId/$tab"
                  params={{ projectId: project.project_id, tab: "activity" }}
                  className="spend-row"
                  key={project.project_id}
                >
                  {row}
                </Link>
              );
            })}
          </div>
          {!data.cost.by_project.length && <div className="panel-empty compact">No metered spend or subscription activity yet.</div>}
          {/* No token total here — the Cost panel owns that, scoped to its
              provider filter. A fleet-wide count under per-project money rows
              said nothing about any of them. */}
          <CostGaps gaps={data.cost.gaps} />
        </Panel>

        <Panel title="Recent episodes">
          {data.recent_episodes.slice(0, 8).map((episode) => (
            <Link
              className="episode-row"
              to="/p/$projectId/ep/$episodeId"
              params={{ projectId: episode.project_id!, episodeId: episode.episode_id }}
              key={`${episode.project_id}-${episode.episode_id}`}
            >
              <StatusBadge state={episode.state} />
              <strong>{episode.goal_id}</strong>
              <span>{episode.feature_id || episode.episode_id}</span>
              <TierBadge tier={episode.tier} agent={episode.agent} />
              <time>{episode.started.slice(11, 19)}</time>
            </Link>
          ))}
          {!data.recent_episodes.length && <div className="panel-empty">No retained episodes.</div>}
        </Panel>

        <Panel title="Stack health">
          <pre className="health-output">{data.stack_health.join("\n")}</pre>
        </Panel>
      </div>
    </div>
  );
}
