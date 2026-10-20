import { Loader2 } from "lucide-react";
import { Suspense, lazy } from "react";
import { Route, Routes } from "react-router";

import { Layout } from "@/components/Layout";

// Pages are code-split; the playground (markdown + syntax highlighting) is the heaviest.
const DashboardPage = lazy(() =>
  import("@/pages/Dashboard").then((m) => ({ default: m.DashboardPage })),
);
const PlaygroundPage = lazy(() =>
  import("@/pages/Playground").then((m) => ({ default: m.PlaygroundPage })),
);
const EmbeddingsPage = lazy(() =>
  import("@/pages/Embeddings").then((m) => ({ default: m.EmbeddingsPage })),
);
const AudioPage = lazy(() => import("@/pages/Audio").then((m) => ({ default: m.AudioPage })));
const ModelsPage = lazy(() => import("@/pages/Models").then((m) => ({ default: m.ModelsPage })));
const ProvidersPage = lazy(() =>
  import("@/pages/Providers").then((m) => ({ default: m.ProvidersPage })),
);
const InfrastructurePage = lazy(() =>
  import("@/pages/Infrastructure").then((m) => ({ default: m.InfrastructurePage })),
);
const BenchmarksPage = lazy(() =>
  import("@/pages/Benchmarks").then((m) => ({ default: m.BenchmarksPage })),
);
const SettingsPage = lazy(() =>
  import("@/pages/Settings").then((m) => ({ default: m.SettingsPage })),
);

function Loading() {
  return (
    <div className="flex h-full items-center justify-center text-muted-foreground">
      <Loader2 className="size-5 animate-spin" aria-label="Loading" />
    </div>
  );
}

export function App() {
  return (
    <Suspense fallback={<Loading />}>
      <Routes>
        <Route element={<Layout />}>
          <Route index element={<DashboardPage />} />
          <Route path="playground" element={<PlaygroundPage />} />
          <Route path="embeddings" element={<EmbeddingsPage />} />
          <Route path="audio" element={<AudioPage />} />
          <Route path="models" element={<ModelsPage />} />
          <Route path="providers" element={<ProvidersPage />} />
          <Route path="infrastructure" element={<InfrastructurePage />} />
          <Route path="benchmarks" element={<BenchmarksPage />} />
          <Route path="settings" element={<SettingsPage />} />
          <Route path="*" element={<DashboardPage />} />
        </Route>
      </Routes>
    </Suspense>
  );
}
