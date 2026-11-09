import { api } from "./api";
import { Badge } from "./components/ui";
import { useHashRoute, usePolling } from "./hooks";
import { DebugRunDetailPage } from "./pages/DebugRunDetailPage";
import { DebugRunsPage } from "./pages/DebugRunsPage";
import { NewDebugRunPage } from "./pages/NewDebugRunPage";
import { RepositoriesPage } from "./pages/RepositoriesPage";

const NAV = [
  { path: "/repositories", label: "Repositories" },
  { path: "/runs/new", label: "New Debug Run" },
  { path: "/runs", label: "Debug Runs" },
];

function HealthIndicator() {
  const { data, error } = usePolling(api.health, () => true, 15000);
  if (error) return <Badge value="error" label="API offline" />;
  if (!data) return null;
  const title = data.ollama.error ?? `Ollama ${data.ollama.version} · pgvector ${data.database.pgvector}`;
  return (
    <span title={title}>
      <Badge value={data.status} label={data.status === "ok" ? "All systems ok" : data.ollama.ok ? data.status : "Ollama unavailable"} />
    </span>
  );
}

export function App() {
  const route = useHashRoute();
  const runMatch = route.match(/^\/runs\/(\d+)$/);

  let page;
  if (runMatch) page = <DebugRunDetailPage runId={Number(runMatch[1])} />;
  else if (route === "/runs/new" || route.startsWith("/runs/new?")) page = <NewDebugRunPage />;
  else if (route === "/runs") page = <DebugRunsPage />;
  else page = <RepositoriesPage />;

  const active = runMatch ? "/runs" : route.split("?")[0];

  return (
    <div className="layout">
      <header className="topbar">
        <div className="brand">
          <span className="logo">◆</span> DebugPilot <span className="muted">local</span>
        </div>
        <nav>
          {NAV.map((item) => (
            <a key={item.path} href={`#${item.path}`} className={active === item.path ? "active" : undefined}>
              {item.label}
            </a>
          ))}
        </nav>
        <HealthIndicator />
      </header>
      <main className="content">{page}</main>
    </div>
  );
}
