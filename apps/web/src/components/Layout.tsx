import { useQuery } from "@tanstack/react-query";
import {
  Boxes,
  Cpu,
  FlaskConical,
  Gauge,
  Layers,
  MessageSquare,
  Moon,
  PlugZap,
  Settings,
  Sun,
} from "lucide-react";
import { useEffect, useState } from "react";
import { NavLink, Outlet } from "react-router";

import { api } from "@/lib/api";
import { cn } from "@/lib/utils";

const NAV = [
  { to: "/", label: "Dashboard", icon: Gauge, end: true },
  { to: "/playground", label: "AI Playground", icon: MessageSquare },
  { to: "/models", label: "Model Registry", icon: Boxes },
  { to: "/providers", label: "Providers", icon: PlugZap },
  { to: "/infrastructure", label: "Infrastructure", icon: Cpu },
  { to: "/benchmarks", label: "Benchmark Lab", icon: FlaskConical },
  { to: "/settings", label: "Settings", icon: Settings },
];

function useTheme(): [boolean, () => void] {
  const [dark, setDark] = useState(() => {
    try {
      const stored = localStorage.getItem("omf.theme");
      if (stored) return stored === "dark";
    } catch {
      // ignore
    }
    return window.matchMedia?.("(prefers-color-scheme: dark)").matches ?? false;
  });
  useEffect(() => {
    document.documentElement.classList.toggle("dark", dark);
    try {
      localStorage.setItem("omf.theme", dark ? "dark" : "light");
    } catch {
      // ignore
    }
  }, [dark]);
  return [dark, () => setDark((d) => !d)];
}

export function Layout() {
  const [dark, toggle] = useTheme();
  const system = useQuery({ queryKey: ["system"], queryFn: api.system, retry: false });
  return (
    <div className="flex h-full">
      <aside className="hidden w-56 shrink-0 flex-col border-r bg-sidebar md:flex">
        <div className="flex items-center gap-2 px-4 py-4">
          <Layers className="size-5 text-[var(--viz-series-1)]" aria-hidden />
          <div>
            <div className="text-sm font-semibold leading-tight">Open Model Fabric</div>
            <div className="text-[11px] text-muted-foreground">
              {system.data
                ? `${system.data.profile} · v${system.data.version}`
                : "provider-neutral AI"}
            </div>
          </div>
        </div>
        <nav className="flex flex-1 flex-col gap-0.5 px-2" aria-label="Main">
          {NAV.map(({ to, label, icon: Icon, end }) => (
            <NavLink
              key={to}
              to={to}
              end={end}
              className={({ isActive }) =>
                cn(
                  "flex items-center gap-2 rounded-md px-2.5 py-1.5 text-sm text-muted-foreground hover:bg-accent hover:text-foreground",
                  isActive && "bg-accent font-medium text-foreground",
                )
              }
            >
              <Icon className="size-4" aria-hidden />
              {label}
            </NavLink>
          ))}
        </nav>
        <div className="flex items-center justify-between border-t px-4 py-3 text-xs text-muted-foreground">
          <span title="Signed-in principal">
            {system.data ? `${system.data.principal} (${system.data.role})` : "not connected"}
          </span>
          <button
            type="button"
            onClick={toggle}
            className="rounded p-1 hover:bg-accent"
            aria-label={dark ? "Switch to light theme" : "Switch to dark theme"}
          >
            {dark ? <Sun className="size-4" /> : <Moon className="size-4" />}
          </button>
        </div>
      </aside>
      <div className="flex min-w-0 flex-1 flex-col">
        <nav
          className="flex gap-1 overflow-x-auto border-b px-2 py-2 md:hidden"
          aria-label="Main (mobile)"
        >
          {NAV.map(({ to, label, end }) => (
            <NavLink
              key={to}
              to={to}
              end={end}
              className={({ isActive }) =>
                cn(
                  "whitespace-nowrap rounded px-2 py-1 text-xs",
                  isActive ? "bg-accent font-medium" : "text-muted-foreground",
                )
              }
            >
              {label}
            </NavLink>
          ))}
        </nav>
        <main className="min-h-0 flex-1 overflow-y-auto">
          <Outlet />
        </main>
      </div>
    </div>
  );
}
