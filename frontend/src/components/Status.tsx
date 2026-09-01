import type { ReactNode } from "react";

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
  return <span className={`badge badge-${normalized}`}>{glyph} {state}</span>;
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
