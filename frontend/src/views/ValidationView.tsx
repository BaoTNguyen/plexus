import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Check, CircleAlert, CircleDashed, GitPullRequest, Play, X } from "lucide-react";
import { useState } from "react";
import { api } from "../api";
import { Panel, StatusBadge } from "../components/Status";
import type { PullRequest } from "../types";

/** Validation: what the machines say, what still needs your eyes, and what is
 *  waiting to reach main.
 *
 *  Automated and manual are kept apart deliberately. A green suite is evidence
 *  that nothing regressed; it is not evidence the right thing was built. A
 *  factory that collapses the two ships something that passes every test and
 *  does the wrong job, and nobody finds out until a user does. */

const CHECK_ICON = {
  passing: <Check size={13} />, failing: <X size={13} />,
  pending: <CircleDashed size={13} />, none: <CircleAlert size={13} />,
};

function PrRow({ pr, root, tasks }: { pr: PullRequest; root: string; tasks: string[] }) {
  const client = useQueryClient();
  const [confirm, setConfirm] = useState(false);
  const merge = useMutation({
    mutationFn: () => api.post("/pr-merge", { root, number: pr.number }),
    onSuccess: () => {
      setConfirm(false);
      client.invalidateQueries({ queryKey: ["validation", root] });
    },
  });
  const blocked = pr.checks === "failing" || pr.draft;
  return (
    <div className="pr-row">
      <GitPullRequest size={15} />
      <div className="pr-copy">
        <strong>
          <a href={pr.url} target="_blank" rel="noreferrer">#{pr.number} {pr.title}</a>
          {pr.draft && <em className="pr-draft">draft</em>}
        </strong>
        <small>
          {pr.head} → {pr.base}
          {/* one PR usually carries several landed tasks; naming them is the
              difference between reading a diff and reviewing decisions */}
          {tasks.length > 0 && <> · carries {tasks.join(", ")}</>}
        </small>
      </div>
      <span className={`pr-checks checks-${pr.checks}`}>
        {CHECK_ICON[pr.checks]} {pr.checks === "none" ? "no CI" : pr.checks}
      </span>
      {confirm ? (
        <span className="pr-confirm">
          <button className="button button-small button-primary"
            disabled={merge.isPending} onClick={() => merge.mutate()}>
            merge to {pr.base}
          </button>
          <button className="button button-small" onClick={() => setConfirm(false)}>cancel</button>
        </span>
      ) : (
        <button className="button button-small" disabled={blocked} onClick={() => setConfirm(true)}
          title={blocked ? "CI is not green, or the PR is a draft" : `Squash-merge into ${pr.base}`}>
          approve
        </button>
      )}
      {merge.error && <span className="inline-error">{merge.error.message}</span>}
    </div>
  );
}

export function ValidationView({ root }: { root: string }) {
  const client = useQueryClient();
  const query = useQuery({
    queryKey: ["validation", root],
    queryFn: () => api.validation(root),
    refetchInterval: 10_000,
  });
  const [checked, setChecked] = useState<string[]>([]);
  const [notes, setNotes] = useState("");
  const validate = useMutation({
    mutationFn: () => api.post("/validate", { root, checks: checked, notes }),
    onSuccess: () => {
      setNotes("");
      client.invalidateQueries({ queryKey: ["validation", root] });
    },
  });
  const data = query.data;
  if (!data) return <div className="loading-grid" aria-label="Loading validation" />;

  const shipped = data.awaiting_signoff.map((t) => t.id);
  return (
    <div className="stack">
      <Panel
        title="Automated ground truth"
        action={<code className="suite-cmd">{data.suite}</code>}
      >
        <div className="validation-summary">
          <StatusBadge state={data.automated.state === "passed" ? "done"
            : data.automated.state === "failed" ? "blocked" : "draft"} />
          <span>
            {data.automated.state === "unknown"
              ? "Not run against this goal yet — the suite is the executable definition of done."
              : `Last run ${data.automated.ts.slice(0, 19).replace("T", " ")}`}
          </span>
        </div>
        {data.landed.length > 0 && (
          <div className="landed-list">
            {data.landed.map((row) => (
              <div className="landed-row" key={`${row.feature_id}-${row.ts}`}>
                <code>{row.commit || "—"}</code>
                <strong>{row.feature_id}</strong>
                <time>{row.ts.slice(0, 19).replace("T", " ")}</time>
              </div>
            ))}
          </div>
        )}
      </Panel>

      <Panel title={`Manual checks · ${data.manual.checks.length}`}>
        {data.manual.checks.length ? (
          <div className="validation-panel">
            {/* The HITL layer. These run on your dev branch, by you, because
                they are the claims no suite can make. */}
            {data.manual.checks.map((check) => (
              <label className="validation-check" key={check}>
                <input
                  type="checkbox"
                  disabled={data.manual.done}
                  checked={data.manual.done || checked.includes(check)}
                  onChange={(event) => setChecked((current) => event.target.checked
                    ? [...current, check] : current.filter((c) => c !== check))}
                />
                <span>{check}</span>
              </label>
            ))}
            {!data.manual.done && (
              <>
                <textarea rows={3} value={notes} onChange={(e) => setNotes(e.target.value)}
                  placeholder="What you actually exercised, and anything that felt wrong" />
                <button
                  className="button button-primary"
                  disabled={checked.length !== data.manual.checks.length || validate.isPending}
                  onClick={() => validate.mutate()}
                >
                  <Check size={14} /> Record sign-off
                </button>
              </>
            )}
            {data.manual.done && (
              <div className="delivery-note">
                Signed off {data.manual.ts.slice(0, 19).replace("T", " ")}
              </div>
            )}
            {validate.error && <div className="inline-error">{validate.error.message}</div>}
          </div>
        ) : (
          <div className="panel-empty compact">
            No manual checks defined. Add them to <code>plexus.toml</code> under{" "}
            <code>[ground_truth].manual</code> — one line per claim the suite cannot make.
          </div>
        )}
      </Panel>

      {data.review_rows.length > 0 && (
        <Panel title={`Plan versus landed · ${data.review_rows.length}`}>
          {data.review_rows.map((row) => (
            <div className="review-row" key={row.id}>
              <span className={`risk risk-${row.risk}`}>{row.risk}</span>
              <strong>{row.title}</strong>
              <code>{row.commit?.slice(0, 12) || "—"}</code>
              <span>{row.state || ""}</span>
            </div>
          ))}
        </Panel>
      )}

      <Panel
        title={`Pull requests · ${data.pull_requests.length}`}
        action={<span>into {data.pr_base || "—"}</span>}
      >
        {data.pull_requests.map((pr) => (
          <PrRow key={pr.number} pr={pr} root={root} tasks={shipped} />
        ))}
        {!data.pull_requests.length && (
          <div className="panel-empty compact">
            Nothing open. A PR appears here once a goal finishes green and pushes.
          </div>
        )}
      </Panel>

      {shipped.length > 0 && (
        <Panel title={`Landed, awaiting your sign-off · ${shipped.length}`}>
          {data.awaiting_signoff.map((task) => (
            <div className="task-signoff" key={task.id}>
              <Play size={12} />
              <strong>{task.title}</strong>
              <code>{task.id}</code>
              {task.pr > 0 && <span>#{task.pr}</span>}
            </div>
          ))}
        </Panel>
      )}
    </div>
  );
}
