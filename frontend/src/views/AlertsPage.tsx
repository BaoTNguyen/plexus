import { useQuery } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";
import { AlertTriangle, ArrowRight } from "lucide-react";
import { api } from "../api";
import { Panel, StatusBadge } from "../components/Status";

export function AlertsPage() {
  const query = useQuery({
    queryKey: ["dashboard"],
    queryFn: () => api.dashboard(),
    refetchInterval: 5_000,
  });
  return (
    <div className="page">
      <div className="page-heading">
        <div><p className="eyebrow">Fleet queue</p><h1>Alerts</h1></div>
      </div>
      <Panel title={`${query.data?.alerts.length || 0} open`}>
        {query.data?.alerts.map((alert, index) => (
          <Link
            className="alert-row alert-row-large"
            to="/p/$projectId/$tab"
            params={{ projectId: alert.project_id, tab: "blocks" }}
            key={`${alert.project_id}-${alert.feature_id}-${index}`}
          >
            <AlertTriangle size={17} />
            <div>
              <strong>{alert.goal_id} · {alert.feature_id || alert.severity}</strong>
              <p>{alert.reason}</p>
            </div>
            <StatusBadge state={alert.severity} />
            <ArrowRight size={15} />
          </Link>
        ))}
        {query.data && !query.data.alerts.length && (
          <div className="panel-empty">No open escalations or stalled projects.</div>
        )}
      </Panel>
    </div>
  );
}
