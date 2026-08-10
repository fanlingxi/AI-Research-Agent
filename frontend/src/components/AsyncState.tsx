import type { ReactNode } from "react";
import { AlertTriangle, Inbox } from "lucide-react";

import { ApiError } from "../lib/api";
import { Button } from "./ui/button";

export function LoadingBlock({ label = "正在读取工作区…" }: { label?: string }) {
  return (
    <div className="animate-pulse rounded-xl border bg-panel p-6 text-sm text-muted-ink" role="status">
      {label}
    </div>
  );
}

export function EmptyBlock({ title, children }: { title: string; children: ReactNode }) {
  return (
    <div className="rounded-xl border border-dashed bg-panel px-6 py-12 text-center">
      <Inbox className="mx-auto mb-3 text-slate-400" size={28} />
      <h3 className="font-medium">{title}</h3>
      <div className="mx-auto mt-2 max-w-md text-sm text-muted-ink">{children}</div>
    </div>
  );
}

function safeErrorMessage(error: Error) {
  if (error instanceof ApiError && error.status === 503) {
    return "相关检索服务当前不可用，请稍后重试。"
  }
  if (error instanceof ApiError && error.status >= 500) {
    return "服务暂时无法完成此请求，请稍后重试。"
  }
  return error.message;
}

export function ErrorBlock({ error, onRetry }: { error: Error; onRetry?: () => void }) {
  return (
    <div className="rounded-xl border border-red-200 bg-danger-soft p-5 text-sm text-red-950" role="alert">
      <div className="flex gap-3">
        <AlertTriangle className="shrink-0" size={19} />
        <div>
          <p className="font-medium">无法完成此操作</p>
          <p className="mt-1 text-red-900/80">{safeErrorMessage(error)}</p>
          {onRetry ? <Button className="mt-3" variant="outline" size="sm" onClick={onRetry}>重试</Button> : null}
        </div>
      </div>
    </div>
  );
}
