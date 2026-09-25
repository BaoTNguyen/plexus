import type { ReactNode } from "react";

/** Words for states whose internal name says nothing to a reader.
 *  "intake" meant "plexus.toml is still the template" to exactly one person. */
const LABEL: Record<string, string> = {
  intake: "not set up",
  draft: "goal written, not planned",
  plan_failed: "plan failed",
  awaiting_approval: "awaiting approval",
};

const HINT: Record<string, string> = {
  intake: "This repo is registered but plexus.toml still holds the template "
    + "goal — write the overview, or run `plexus init` here, before planning.",
  draft: "The goal is written and nothing has been planned from it yet.",
  awaiting_approval: "A plan exists and is waiting for your sign-off.",
};

export function StatusBadge({ state }: { state: string }) {
  const normalized =
    state === "landed" || state === "finished" || state === "passed" || state === "done"
      ? "ok"
      : state === "running" || state === "planning"
        ? "running"
        : state === "failed" || state === "escalated" || state === "blocked" || state === "plan_failed"
          ? "bad"
          : "idle";
  const glyph = { ok: "✔", running: "▶", bad: "✖", idle: "○" }[normalized];
  return (
    <span className={`badge badge-${normalized}`} title={HINT[state]}>
      {glyph} {LABEL[state] || state.replaceAll("_", " ")}
    </span>
  );
}

export function TierBadge({ tier, agent }: { tier?: string; agent?: string }) {
  const glyph = tier === "strong" ? "●" : tier === "standard" ? "◐" : "○";
  return (
    <span className={`tier tier-${tier || "unknown"}`}>
      {glyph} {tier || "unrouted"}{agent ? ` / ${agent}` : ""}
    </span>
  );
}

export function Panel({
  title,
  action,
  children,
  className = "",
}: {
  title: string;
  action?: ReactNode;
  children: ReactNode;
  className?: string;
}) {
  return (
    <section className={`panel ${className}`}>
      <div className="panel-heading">
        <h2>{title}</h2>
        {action}
      </div>
      {children}
    </section>
  );
}
