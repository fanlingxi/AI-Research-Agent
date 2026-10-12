import type { ReactNode } from "react";
import { AlertTriangle, Inbox } from "lucide-react";

import { ApiError } from "../lib/api";
import { cn } from "../lib/utils";
import { Button } from "./ui/button";

export function LoadingBlock({ label = "正在读取…", className }: { label?: string; className?: string }) {
  return (
    <div className={cn("rounded-xl border bg-panel p-5", className)} role="status" aria-live="polite">
      <div aria-hidden="true" className="animate-pulse space-y-3">
        <div className="h-3 w-1/3 rounded bg-subtle" />
        <div className="h-3 w-5/6 rounded bg-subtle" />
        <div className="h-3 w-2/3 rounded bg-subtle" />
      </div>
      <p className="mt-4 text-xs text-muted-ink">{label}</p>
    </div>
  );
}

export function EmptyBlock({
  title,
  children,
  icon,
  action,
  className,
}: {
  title: string;
  children?: ReactNode;
  icon?: ReactNode;
  action?: ReactNode;
  className?: string;
}) {
  return (
    <div className={cn("rounded-xl border border-dashed border-line-strong bg-panel/60 px-6 py-10 text-center", className)}>
      <div className="mx-auto mb-3 grid size-10 place-items-center rounded-full bg-subtle text-muted-ink">
        {icon ?? <Inbox size={19} />}
      </div>
      <h3 className="text-[15px] font-medium text-ink">{title}</h3>
      {children ? <div className="mx-auto mt-1.5 max-w-md text-[13px] leading-6 text-muted-ink">{children}</div> : null}
      {action ? <div className="mt-4 flex justify-center">{action}</div> : null}
    </div>
  );
}

function safeErrorMessage(error: Error) {
  if (error instanceof ApiError && error.status === 503) {
    return "相关检索服务当前不可用，请稍后重试。";
  }
  if (error instanceof ApiError && error.status >= 500) {
    return "服务暂时无法完成此请求，请稍后重试。";
  }
  if (error instanceof TypeError || /failed to fetch|networkerror/i.test(error.message)) {
    return "无法连接到工作台服务，请确认后端已启动。";
  }
  return error.message;
}

export function ErrorBlock({ error, onRetry, title = "无法完成此操作" }: { error: Error; onRetry?: () => void; title?: string }) {
  return (
    <div className="rounded-xl border border-red-200 bg-danger-soft p-4 text-sm text-red-950" role="alert">
      <div className="flex gap-3">
        <AlertTriangle className="mt-0.5 shrink-0 text-red-700" size={17} />
        <div className="min-w-0">
          <p className="font-medium">{title}</p>
          <p className="mt-0.5 break-words text-red-900/80">{safeErrorMessage(error)}</p>
          {onRetry ? <Button className="mt-3" variant="outline" size="sm" onClick={onRetry}>重试</Button> : null}
        </div>
      </div>
    </div>
  );
}

/** Inline notice: neutral explanation, warning, or success feedback. */
export function Notice({ tone = "info", title, children, icon, className, action }: {
  tone?: "info" | "warning" | "success" | "danger";
  title?: ReactNode;
  children?: ReactNode;
  icon?: ReactNode;
  className?: string;
  action?: ReactNode;
}) {
  const tones = {
    info: "border-line bg-subtle/70 text-ink-soft",
    warning: "border-amber-200 bg-warning-soft text-amber-950",
    success: "border-emerald-200 bg-success-soft text-emerald-950",
    danger: "border-red-200 bg-danger-soft text-red-950",
  };
  return (
    <div className={cn("flex min-w-0 flex-wrap items-start gap-3 rounded-lg border px-3.5 py-3 text-[13px] leading-6", tones[tone], className)} role={tone === "danger" ? "alert" : "status"}>
      {icon ? <span className="mt-1 shrink-0">{icon}</span> : null}
      <div className="min-w-0 flex-1">
        {title ? <p className="font-medium">{title}</p> : null}
        {children ? <div className={cn(title && "mt-0.5", "opacity-90")}>{children}</div> : null}
      </div>
      {action ? <div className="shrink-0">{action}</div> : null}
    </div>
  );
}
