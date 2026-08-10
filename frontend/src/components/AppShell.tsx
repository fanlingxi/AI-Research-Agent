import { useQuery } from "@tanstack/react-query";
import { FolderKanban, LayoutDashboard, Plus, SearchCheck, ShieldCheck } from "lucide-react";
import type { ReactNode } from "react";
import { Link, NavLink, useLocation } from "react-router-dom";

import { api } from "../lib/api";
import { cn } from "../lib/utils";
import { Button } from "./ui/button";

function MainLink({ to, label, icon }: { to: string; label: string; icon: ReactNode }) {
  return (
    <NavLink
      to={to}
      className={({ isActive }) =>
        cn(
          "flex items-center gap-2 rounded-lg px-3 py-2 text-sm font-medium text-slate-600 transition hover:bg-slate-100 hover:text-ink",
          isActive && "bg-brand-soft text-brand",
        )
      }
    >
      {icon}
      {label}
    </NavLink>
  );
}

export function AppShell({ children }: { children: ReactNode }) {
  const projects = useQuery({ queryKey: ["projects"], queryFn: api.projects });
  const location = useLocation();

  return (
    <div className="min-h-screen lg:grid lg:grid-cols-[250px_minmax(0,1fr)]">
      <aside className="border-b bg-panel lg:sticky lg:top-0 lg:h-screen lg:border-b-0 lg:border-r">
        <div className="flex h-full flex-col p-4">
          <Link to="/" className="mb-7 flex items-center gap-2 px-2 text-[15px] font-semibold tracking-tight">
            <span className="grid size-8 place-items-center rounded-lg bg-brand text-white">
              <ShieldCheck size={18} />
            </span>
            Research Workspace
          </Link>

          <nav className="space-y-1">
            <MainLink to="/" label="Dashboard" icon={<LayoutDashboard size={17} />} />
            <MainLink to="/review" label="Review queue" icon={<SearchCheck size={17} />} />
          </nav>

          <div className="mt-8 flex items-center justify-between px-2">
            <span className="text-xs font-semibold uppercase tracking-[0.12em] text-muted-ink">Projects</span>
            <Link aria-label="创建项目" to="/?createProject=1">
              <Button size="sm" variant="ghost" className="size-7 px-0">
                <Plus size={16} />
              </Button>
            </Link>
          </div>
          <div className="mt-2 min-h-0 space-y-1 overflow-y-auto">
            {projects.isPending ? <p className="px-2 py-3 text-xs text-muted-ink">载入项目…</p> : null}
            {projects.data?.map((project) => {
              const active = location.pathname.startsWith(`/projects/${project.id}`);
              return (
                <Link
                  key={project.id}
                  to={`/projects/${project.id}/overview`}
                  className={cn(
                    "flex items-center gap-2 rounded-lg px-2 py-2 text-sm text-slate-600 hover:bg-slate-100",
                    active && "bg-brand-soft font-medium text-brand",
                  )}
                >
                  <FolderKanban size={16} className="shrink-0" />
                  <span className="truncate">{project.name}</span>
                </Link>
              );
            })}
          </div>
          <p className="mt-auto hidden border-t px-2 pt-4 text-xs leading-5 text-muted-ink lg:block">
            SQLite facts · scoped ContextSnapshot · reviewed Memory
          </p>
        </div>
      </aside>
      <main className="min-w-0">{children}</main>
    </div>
  );
}
