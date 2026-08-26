import { useQuery } from "@tanstack/react-query";
import { Link, useParams } from "@tanstack/react-router";
import { ArrowLeft, Database, GitBranch, ListTree } from "lucide-react";
import { api } from "../api";
import { Panel, StatusBadge, TierBadge } from "../components/Status";

export function EpisodePage() {
  const { projectId, episodeId } = useParams({ from: "/p/$projectId/ep/$episodeId" });
  const goals = useQuery({ queryKey: ["goals"], queryFn: api.goals });
  const goal = goals.data?.find((item) => item.project_id === projectId);
  const detail = useQuery({
    queryKey: ["episode", goal?.root, episodeId],
    queryFn: () => api.episode(goal!.root, episodeId),
    enabled: Boolean(goal),
    refetchInterval: (query) => query.state.data?.meta.state === "running" ? 2_000 : false,
  });
  if (!detail.data) return <div className="loading-grid" />;
  const { meta } = detail.data;
  return (
    <div className="page episode-detail">
      <Link to="/p/$projectId/$tab" params={{ projectId, tab: "episodes" }} className="back-link">
        <ArrowLeft size={14} /> episodes
      </Link>
      <div className="project-heading">
        <div>
          <p className="eyebrow">{goal?.goal_id} · {meta.task_id}</p>
          <h1>Episode {meta.episode_id}</h1>
          <span>{meta.feature_id} · attempt {meta.attempt}</span>
        </div>
        <div className="episode-meta">
          <StatusBadge state={meta.state} />
          <TierBadge tier={meta.tier} agent={meta.agent} />
          <strong>${meta.cost_usd.toFixed(2)}</strong>
        </div>
      </div>

      <Panel title="Orchestration" action={<span>{meta.verify_rounds} verifier rounds</span>}>
        <div className="orchestration">
          {detail.data.route.length ? detail.data.route.map((route, index) => (
            <div className="route-row" key={`${route.ts}-${index}`}>
              <GitBranch size={15} />
              <span>{route.ts.slice(11, 19)}</span>
              <TierBadge
                tier={String(route.payload?.tier || meta.tier)}
                agent={String(route.payload?.agent || route.payload?.chosen || meta.agent)}
              />
              <span>{route.detail}</span>
            </div>
          )) : <div className="panel-empty compact">No retained routing event.</div>}
        </div>
      </Panel>

      <div className="episode-columns">
        <Panel title={`Steps · ${detail.data.steps.length}`} action={<ListTree size={15} />}>
          <div className="step-list">
            {detail.data.steps.map((step, index) => (
              <div className="step-row" key={`${step.ts}-${index}`}>
                <time>{step.ts.slice(11, 19)}</time>
                <span className={`source source-${step.source}`}>{step.source}</span>
                <strong>{step.kind}</strong>
                <span>{step.detail}</span>
              </div>
            ))}
          </div>
        </Panel>
        <Panel title={`Memory ledger · ${detail.data.memory.length}`} action={<Database size={15} />}>
          {detail.data.memory.map((row, index) => (
            <div className="memory-row" key={`${row.ts}-${index}`}>
              <span className={`source source-${row.source}`}>{row.source}</span>
              <strong>{String(row.payload?.level || "?")}</strong>
              <span>{row.kind}</span>
              <time>{row.ts.slice(11, 19)}</time>
              <small>{row.detail}</small>
            </div>
          ))}
          {!detail.data.memory.length && (
            <div className="panel-empty">
              No retained memory events. Level remains unknown until emitters include it.
            </div>
          )}
        </Panel>
      </div>
    </div>
  );
}
