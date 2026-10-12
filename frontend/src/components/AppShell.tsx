import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import * as DialogPrimitive from "@radix-ui/react-dialog";
import {
  Activity,
  FileText,
  FlaskConical,
  FolderKanban,
  House,
  Library,
  ListChecks,
  Menu,
  Network,
  Telescope,
} from "lucide-react";
import { useEffect, useState, type ReactNode } from "react";
import { Link, NavLink, useLocation, useNavigate, useSearchParams } from "react-router-dom";

import { api } from "../lib/api";
import { usePendingCandidates, useSystemStatus } from "../lib/hooks";
import { cn } from "../lib/utils";
import { ThemeToggle } from "./ThemeToggle";
import { ErrorBlock } from "./AsyncState";
import { Button } from "./ui/button";
import { Dialog, DialogContent, DialogDescription, DialogTitle, SheetContent } from "./ui/dialog";
import { Field, Input, Textarea } from "./ui/field";

function NavItem({ to, label, icon, end, badge, quiet }: { to: string; label: string; icon: ReactNode; end?: boolean; badge?: number; quiet?: boolean }) {
  return (
    <NavLink
      end={end}
      to={to}
      className={({ isActive }) =>
        cn(
          "group relative flex h-9 items-center gap-2.5 rounded-lg px-2.5 text-sm font-medium transition-colors",
          quiet ? "text-muted-ink hover:bg-subtle hover:text-ink" : "text-ink-soft hover:bg-subtle hover:text-ink",
          isActive && "bg-brand-soft text-brand-strong hover:bg-brand-soft hover:text-brand-strong",
        )
      }
    >
      <span className="shrink-0 opacity-90">{icon}</span>
      <span className="truncate">{label}</span>
      {badge ? (
        <span className="ml-auto rounded-full bg-amber-100 px-1.5 text-[11px] font-semibold leading-5 tabular-nums text-amber-900">
          {badge > 999 ? "999+" : badge}
        </span>
      ) : null}
    </NavLink>
  );
}

function SystemStatus() {
  const health = useSystemStatus();
  if (!health.data) return null;
  const workerOnline = health.data.services.worker?.available === true;
  const llmReady = health.data.knowledge.live_llm_configured;
  const ok = workerOnline && llmReady;
  const message = ok ? "后台服务正常" : !workerOnline ? "后台执行器未运行" : "未配置语言模型";
  return (
    <Link
      to="/runtime"
      className="flex items-center gap-2 rounded-lg px-2.5 py-2 text-xs text-muted-ink hover:bg-subtle hover:text-ink"
      title={ok ? "导入与研究任务可以正常执行" : "新的导入和研究任务暂时无法执行，点击查看详情"}
    >
      <span className={cn("size-2 shrink-0 rounded-full", ok ? "bg-emerald-500" : "bg-amber-500")} />
      <span className="truncate">{message}</span>
    </Link>
  );
}

function SidebarContent({ onNavigate }: { onNavigate?: () => void }) {
  const projects = useQuery({ queryKey: ["projects"], queryFn: api.projects });
  const pending = usePendingCandidates();
  const dashboard = useQuery({ queryKey: ["dashboard"], queryFn: api.dashboard, staleTime: 30_000 });
  const location = useLocation();
  const reviewCount = pending.total + (dashboard.data?.pending_memory_proposals.length ?? 0);
  const activeProjects = (projects.data ?? []).filter((project) => project.status !== "archived").slice(0, 6);
  return (
    <div className="flex h-full min-h-0 flex-col" onClick={(event) => { if ((event.target as HTMLElement).closest("a")) onNavigate?.(); }}>
      <Link to="/" className="flex h-14 shrink-0 items-center gap-2.5 px-4">
        <span className="grid size-8 place-items-center rounded-lg bg-brand text-on-brand shadow-[var(--shadow-card)]">
          <Telescope size={17} />
        </span>
        <span className="text-[15px] font-semibold tracking-tight text-ink">研究工作台</span>
      </Link>

      <nav aria-label="主导航" className="scroll-thin min-h-0 flex-1 space-y-0.5 overflow-y-auto px-3 pb-4 pt-2">
        <NavItem end to="/" label="首页" icon={<House size={17} />} />
        <NavItem end to="/knowledge" label="知识库" icon={<Library size={17} />} />
        <NavItem to="/knowledge/map" label="知识图谱" icon={<Network size={17} />} />
        <NavItem to="/review" label="审核" icon={<ListChecks size={17} />} badge={reviewCount || undefined} />
        <NavItem to="/reports" label="报告" icon={<FileText size={17} />} />
        <NavItem end to="/projects" label="项目" icon={<FolderKanban size={17} />} />
        {activeProjects.length ? (
          <div className="ml-[18px] space-y-0.5 border-l pl-2.5 pt-0.5">
            {activeProjects.map((project) => {
              const active = location.pathname.startsWith(`/projects/${project.id}`);
              return (
                <Link
                  key={project.id}
                  to={`/projects/${project.id}/overview`}
                  title={project.name}
                  className={cn(
                    "block truncate rounded-md px-2 py-1.5 text-[13px] text-muted-ink hover:bg-subtle hover:text-ink",
                    active && "bg-subtle font-medium text-ink",
                  )}
                >
                  {project.name}
                </Link>
              );
            })}
          </div>
        ) : null}

        <p className="px-2.5 pb-1 pt-6 text-xs font-medium text-muted-ink/80">高级</p>
        <NavItem quiet to="/runtime" label="运行记录" icon={<Activity size={16} />} />
        <NavItem quiet to="/experiments" label="实验对比" icon={<FlaskConical size={16} />} />
      </nav>

      <div className="shrink-0 border-t px-3 py-2">
        <ThemeToggle />
        <SystemStatus />
      </div>
    </div>
  );
}

export function CreateProjectDialog() {
  const [params, setParams] = useSearchParams();
  const navigate = useNavigate();
  const client = useQueryClient();
  const [name, setName] = useState("");
  const [goal, setGoal] = useState("");
  const [domain, setDomain] = useState("");
  const close = () => setParams((current) => {
    const next = new URLSearchParams(current);
    next.delete("createProject");
    return next;
  });
  const create = useMutation({
    mutationFn: () => api.createProject({ name: name.trim(), goal: goal.trim(), domain: domain.trim() }),
    onSuccess: (project) => {
      void client.invalidateQueries({ queryKey: ["projects"] });
      void client.invalidateQueries({ queryKey: ["dashboard"] });
      setName(""); setGoal(""); setDomain("");
      navigate(`/projects/${project.id}/overview`);
    },
  });
  const open = params.get("createProject") === "1";
  return (
    <Dialog open={open} onOpenChange={(next) => !next && close()}>
      <DialogContent>
        <DialogTitle>新建项目</DialogTitle>
        <DialogDescription>项目用来组织一个长期研究主题：它有自己的任务、报告和记忆。创建后再选择项目可以使用哪些知识集合。</DialogDescription>
        <form className="mt-5 space-y-4" onSubmit={(event) => { event.preventDefault(); create.mutate(); }}>
          <Field label="项目名称"><Input autoFocus minLength={2} required value={name} onChange={(event) => setName(event.target.value)} placeholder="例如：RAG 检索改进路线" /></Field>
          <Field label="研究目标" hint="可选。写清楚想回答的问题，后续任务会以它为背景。"><Textarea className="min-h-24" value={goal} onChange={(event) => setGoal(event.target.value)} /></Field>
          <Field label="领域（可选）"><Input value={domain} onChange={(event) => setDomain(event.target.value)} placeholder="例如：信息检索" /></Field>
          {create.error instanceof Error ? <ErrorBlock error={create.error} /> : null}
          <div className="flex justify-end gap-2 pt-1"><Button variant="outline" onClick={close}>取消</Button><Button disabled={create.isPending} type="submit">{create.isPending ? "创建中…" : "创建项目"}</Button></div>
        </form>
      </DialogContent>
    </Dialog>
  );
}

export function AppShell({ children }: { children: ReactNode }) {
  const [menuOpen, setMenuOpen] = useState(false);
  const location = useLocation();
  useEffect(() => { setMenuOpen(false); }, [location.pathname]);
  return (
    <div className="min-h-screen lg:grid lg:grid-cols-[232px_minmax(0,1fr)]">
      <aside className="hidden border-r bg-panel lg:sticky lg:top-0 lg:block lg:h-screen">
        <SidebarContent />
      </aside>

      <div className="sticky top-0 z-30 flex h-14 items-center justify-between border-b bg-panel/95 px-4 backdrop-blur lg:hidden">
        <Link to="/" className="flex items-center gap-2 text-[15px] font-semibold">
          <span className="grid size-7 place-items-center rounded-lg bg-brand text-on-brand"><Telescope size={15} /></span>
          研究工作台
        </Link>
        <DialogPrimitive.Root open={menuOpen} onOpenChange={setMenuOpen}>
          <Button aria-label="打开导航" size="icon" variant="ghost" onClick={() => setMenuOpen(true)}><Menu size={20} /></Button>
          <SheetContent side="left" label="导航" className="w-[min(280px,85vw)]">
            <DialogPrimitive.Title className="sr-only">导航</DialogPrimitive.Title>
            <SidebarContent onNavigate={() => setMenuOpen(false)} />
          </SheetContent>
        </DialogPrimitive.Root>
      </div>

      <main className="min-w-0">{children}</main>
      <CreateProjectDialog />
    </div>
  );
}
