export type Goal = {
  root: string;
  name: string;
  project_id: string;
  goal_id: string;
  goal_state: GoalState;
  label: string;
  pinned: boolean;
  running: boolean;
  status: string;
  code: number;
  /** tmux session backing this project's terminal, e.g. plexus-plexus */
  term_session: string;
};

export type Fleet = {
  local_slots: number;
  global_agents: number;
  max_goals: number;
  /** false when tmux is missing; the terminal tab is hidden */
  terminal: boolean;
};

export type Feature = {
  id: string;
  title: string;
  acceptance: string;
  state: string;
  attempt: number;
  budget_used: number;
  priority: number;
  depends_on: string[];
  manual_checks: string[];
};

export type Escalation = {
  feature_id: string;
  reason_class: string;
  reason: string;
};

export type GoalDetail = {
  goal_id: string;
  root: string;
  approved: boolean;
  lifecycle: GoalLifecycle;
  running: boolean;
  status: string;
  insights: string[];
  features: Feature[];
  escalations: Escalation[];
  activity: Array<{ ts: string; kind: string; feature_id: string; reason: string }>;
  why: string[];
};

export type GoalState =
  | "intake"
  | "clarify"
  | "draft"
  | "planning"
  | "plan_failed"
  | "awaiting_approval"
  | "ready"
  | "running"
  | "blocked"
  | "validating"
  | "done";

export type GoalLifecycle = {
  state: GoalState;
  approved: boolean;
  plan_id: string;
  plan_exists: boolean;
  features: number;
  editable: boolean;
  job: {
    kind: string;
    running: boolean;
    exit_code: number | null;
    finished: string;
  };
  validation: {
    automated_passed: boolean;
    manual_passed: boolean;
    checks: string[];
  };
  delivery: string;
};

/** One agent invocation against a feature, folded from its spine events. */
export type Episode = {
  episode_id: string;
  task_id: string;
  feature_id: string;
  attempt: number;
  state: string;
  started: string;
  finished: string;
  duration_ms: number | null;
  tier: string;
  agent: string;
  cost_usd: number;
  verify_rounds: number;
  outcome: string;
  /** stamped on by the dashboard when episodes from several repos are merged */
  project_id?: string;
  goal_id?: string;
};

/** A row in the live tail: one spine event, flattened for display. */
export type EventRow = {
  ts: string;
  source: string;
  kind: string;
  detail: string;
  role?: string;
  duration_ms?: number | null;
  /** only on episode detail rows; the live tail sends the flattened `detail` */
  payload?: Record<string, unknown>;
};

export type EpisodeDetail = {
  meta: Episode;
  steps: EventRow[];
  memory: EventRow[];
  route: EventRow[];
  verify: EventRow[];
};

export type Alert = Escalation & {
  severity: "blocked" | "stalled";
  project_id: string;
  goal_id: string;
};

/** A provider's rates, optionally overridden per model. Opus and Haiku on one
 *  CLI differ by more than 10x, so the provider rate is a fallback, not the
 *  answer. `models` is empty on a config that predates per-model rates. */
export type ProviderPricing = {
  input: number;
  output: number;
  models?: Record<string, { input: number; output: number }>;
};

export type Dashboard = {
  cost: {
    total: number;
    seven_day: number;
    subscription: number;
    metered_api: number;
    equivalent_api: number;
    /** equivalent_api / subscription. >1 means the seats beat pay-as-you-go.
     *  null when no seat cost is configured, which is not the same as 0. */
    seat_utilisation: number | null;
    /** per-subscription value vs accrued cost. Split because a pooled ratio is
     *  dominated by whichever plan costs most, not whichever does the work. */
    by_provider: Array<{
      provider: string;
      monthly: number;
      /** what this plan actually costs in the window. A subscription accrues
       *  whether used or not, so that is its cost — unlike `equivalent`, which
       *  is what the same work would have cost on metered API. */
      cost: number;
      equivalent: number;
      tokens_in: number;
      tokens_out: number;
      cache_tokens: number;
      /** in + out + cache, for the panel's headline figure */
      tokens: number;
    }>;
    /** cache read + write tokens; billed at multipliers off the input rate, so
     *  deliberately not summed into tokens_in */
    cache_tokens: number;
    subscriptions: { claude: number; codex: number };
    /** Seat cost inferred from the signed-in CLI plans. A provider is absent
     *  when no plan could be read, which is why it is partial, not zeroed. */
    detected_subscriptions: Partial<Record<"claude" | "codex", number>>;
    pricing: {
      claude: ProviderPricing;
      codex: ProviderPricing;
    };
    /** Turns no money could be attached to, by reason. Kept apart from turns
     *  that genuinely cost nothing — both render as $0.00 if you only sum
     *  money, which is how a provider with no adapter reads as free.
     *    unmeasured    arteries had no transcript and no declared counts
     *    unattributed  real tokens, but no `cli` to choose a rate card
     *    unpriced      known provider, but no usable rate card row */
    gaps: Partial<Record<"unmeasured" | "unattributed" | "unpriced", number>>;
    /** models actually seen per provider, with turn counts — the input for
     *  deciding whether a per-model rate is worth configuring */
    models: Record<string, Record<string, number>>;
    local: { turns: number; tokens: number; duration_ms: number };
    plexus_attributed: {
      cost_usd: number;
      seven_day: number;
      tokens_in: number;
      tokens_out: number;
    };
    by_project: Array<{
      project_id: string;
      name: string;
      /** money that exists because a turn ran — metered API only */
      marginal_usd: number;
      /** this project's share of seat cost owed regardless; an allocation,
       *  not a charge, so it is shown beside marginal and never added into it */
      seat_usd: number;
      cost_usd: number;
      unassigned: boolean;
    }>;
    window_h: number;
    tokens_in: number;
    tokens_out: number;
  };
  runs: {
    running: number;
    blocked: number;
    stalled: number;
    landed: number;
    landed_7d: number;
    projects: number;
  };
  alerts: Alert[];
  recent_episodes: Episode[];
  activity: {
    events: number;
    turns: number;
    responses: number;
    retrievals: number;
    active_5m: number;
    last_event: string;
    tokens_in: number;
    tokens_out: number;
    cache_read: number;
    cache_write: number;
    metered_turns: number;
    by_source: Array<{ source: string; events: number }>;
  };
  stack_health: string[];
};

export type AccountingConfig = {
  subscriptions: { claude: number; codex: number };
  pricing: {
    claude: { input: number; output: number };
    codex: { input: number; output: number };
  };
};

/** Live tmux windows in a project's session, plus transcripts of runs whose
 *  windows have already closed. The build tab switches over both. */
export type TermWindows = {
  session: string;
  windows: { id: string; name: string; active: boolean; activity: string }[];
  transcripts: { name: string; file: string; bytes: number; finished: string }[];
};

/** A unit of work between the charter and an episode. Sourced by hand or from
 *  a GitHub issue; runs only when every task it is blocked by has landed. */
export type Task = {
  id: string;
  title: string;
  body: string;
  source_kind: "manual" | "github";
  source_url: string;
  state: "open" | "planning" | "ready" | "running" | "landed" | "closed";
  blocked_by: string[];
  requires_plan: boolean;
  plan_id: string;
  order: number;
  created: string;
  updated: string;
  reason: string;
  error: string;
  pr: number;
  design_types: string[];
  design_interfaces: string[];
  design_call_paths: string[];
  waiting_on: string[];
  runnable: boolean;
  needs_plan: boolean;
};

export type TaskBoard = {
  tasks: Task[];
  next: string;
  in_flight: string[];
  /** the four buckets the board renders, sorted by what you want to know */
  active: Task[];
  blocked: Task[];
  planned: Task[];
  done: Task[];
};

export type PullRequest = {
  number: number; title: string; head: string; base: string; url: string;
  draft: boolean; mergeable: string; checks: "passing" | "failing" | "pending" | "none";
};

export type Validation = {
  suite: string;
  automated: { state: "passed" | "failed" | "unknown"; ts: string };
  manual: { checks: string[]; done: boolean; ts: string };
  landed: { feature_id: string; ts: string; commit: string }[];
  awaiting_signoff: Task[];
  review_rows: { id: string; title: string; risk: string; commit?: string; state?: string }[];
  pull_requests: PullRequest[];
  pr_base: string;
};

/** One standing document on the overview. Markdown, because architecture is a
 *  diagram and program design is a signature, and neither fits a string list. */
export type OverviewSection = {
  key: string;
  title: string;
  /** what belongs here — shown as placeholder text and sent to the agent */
  brief: string;
  text: string;
  updated: string;
};

export type Overview = { sections: OverviewSection[]; assets: string[] };

/** Live tmux sessions for a project: the shell, plus any open conversation. */
export type TermSessions = { sessions: { view: string; name: string }[]; base: string };
