import { useQuery } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";
import { api } from "../api";
import { Panel, StatusBadge, TierBadge } from "../components/Status";
import { BuildView } from "./BuildView";

/** Runs: implementation as it happens.
 *
 *  Terminal first, because that is where coding happens whether you are
 *  driving or an agent is — episodes launch into windows of this project's tmux
 *  session, so watching one and taking one over are the same act. What is in
 *  flight sits above it, and the ledger tail below, so the raw output and the
 *  structured record of the same run read side by side. */
export function RunsView({ root, projectId, sessionName, log }: {
  root: string;
  projectId: string;
  sessionName: string;
  log: React.ReactNode;
}) {
  const board = useQuery({
    queryKey: ["tasks", root],
    queryFn: () => api.tasks(root),
    refetchInterval: 5_000,
  });
  const episodes = useQuery({
    queryKey: ["episodes", root],
    queryFn: () => api.episodes(root),
    refetchInterval: 5_000,
  });
  const live = episodes.data?.filter((e) => e.state === "running") ?? [];
  const active = board.data?.active ?? [];
  const next = board.data?.next ?? "";

  return (
    <div className="stack">
      <Panel
        title={`In flight · ${active.length}`}
        action={next ? <span>next: {next}</span> : <span>queue empty</span>}
      >
        {active.map((task) => (
          <div className="run-row" key={task.id}>
            <StatusBadge state={task.state} />
            <strong>{task.title}</strong>
            <code>{task.id}</code>
            {task.plan_id && <span>plan {task.plan_id}</span>}
          </div>
        ))}
        {live.map((episode) => (
          <Link
            className="episode-row episode-row-wide"
            to="/p/$projectId/ep/$episodeId"
            params={{ projectId, episodeId: episode.episode_id }}
            key={episode.episode_id}
          >
            <span className="pulse" />
            <strong>{episode.feature_id || episode.episode_id}</strong>
            <span>a{episode.attempt}</span>
            <TierBadge tier={episode.tier} agent={episode.agent} />
            <span>${episode.cost_usd.toFixed(2)}</span>
          </Link>
        ))}
        {!active.length && !live.length && (
          <div className="panel-empty compact">Nothing running. Start work from the terminal below, or from a task.</div>
        )}
      </Panel>

      <BuildView root={root} sessionName={sessionName} />
      {log}
    </div>
  );
}
