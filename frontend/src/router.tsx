import {
  createHashHistory,
  createRootRoute,
  createRoute,
  createRouter,
  lazyRouteComponent,
  Outlet,
} from "@tanstack/react-router";
import { AppShell } from "./shell/AppShell";

const rootRoute = createRootRoute({
  component: () => (
    <AppShell>
      <Outlet />
    </AppShell>
  ),
  notFoundComponent: () => <div className="empty">That control-plane view does not exist.</div>,
});

const dashboardRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/",
  component: lazyRouteComponent(() => import("./views/DashboardPage"), "DashboardPage"),
});

const alertsRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/alerts",
  component: lazyRouteComponent(() => import("./views/AlertsPage"), "AlertsPage"),
});

const projectRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/p/$projectId/$tab",
  component: lazyRouteComponent(() => import("./views/ProjectPage"), "ProjectPage"),
});

const episodeRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/p/$projectId/ep/$episodeId",
  component: lazyRouteComponent(() => import("./views/EpisodePage"), "EpisodePage"),
});

const routeTree = rootRoute.addChildren([
  dashboardRoute,
  alertsRoute,
  projectRoute,
  episodeRoute,
]);

export const router = createRouter({
  routeTree,
  history: createHashHistory(),
  defaultPreload: "intent",
});

declare module "@tanstack/react-router" {
  interface Register {
    router: typeof router;
  }
}
