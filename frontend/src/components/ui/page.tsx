import { ChevronDown } from "lucide-react";
import type { ReactNode } from "react";

import { cn } from "../../lib/utils";

/** Standard page wrapper: centred column, consistent gutters at every width. */
export function Page({ children, className, width = "default" }: { children: ReactNode; className?: string; width?: "default" | "narrow" | "wide" }) {
  return (
    <div
      className={cn(
        "mx-auto w-full min-w-0 px-4 pb-16 pt-6 sm:px-6 lg:px-10 lg:pt-9",
        width === "narrow" ? "max-w-4xl" : width === "wide" ? "max-w-[1400px]" : "max-w-6xl",
        className,
      )}
    >
      {children}
    </div>
  );
}

export function PageHeader({
  title,
  description,
  actions,
  eyebrow,
  children,
}: {
  title: ReactNode;
  description?: ReactNode;
  actions?: ReactNode;
  eyebrow?: ReactNode;
  children?: ReactNode;
}) {
  return (
    <header className="mb-7 min-w-0">
      {eyebrow ? <div className="mb-2 text-[13px] text-muted-ink">{eyebrow}</div> : null}
      <div className="flex min-w-0 flex-wrap items-start justify-between gap-x-6 gap-y-4">
        <div className="min-w-0 max-w-3xl">
          <h1 className="break-words text-[22px] font-semibold leading-8 tracking-tight text-ink sm:text-2xl">{title}</h1>
          {description ? <p className="mt-1.5 text-sm leading-6 text-muted-ink">{description}</p> : null}
        </div>
        {actions ? <div className="flex flex-wrap items-center gap-2">{actions}</div> : null}
      </div>
      {children}
    </header>
  );
}

export function SectionHeading({ title, description, actions, className }: { title: ReactNode; description?: ReactNode; actions?: ReactNode; className?: string }) {
  return (
    <div className={cn("mb-3 flex min-w-0 flex-wrap items-end justify-between gap-3", className)}>
      <div className="min-w-0">
        <h2 className="text-[15px] font-semibold text-ink">{title}</h2>
        {description ? <p className="mt-0.5 text-[13px] leading-5 text-muted-ink">{description}</p> : null}
      </div>
      {actions ? <div className="flex flex-wrap items-center gap-2">{actions}</div> : null}
    </div>
  );
}

/** Collapsible "详情" area for technical detail that should not compete with the main content. */
export function Disclosure({
  summary,
  children,
  className,
  defaultOpen,
}: {
  summary: ReactNode;
  children: ReactNode;
  className?: string;
  defaultOpen?: boolean;
}) {
  return (
    <details className={cn("group min-w-0 rounded-lg border bg-panel", className)} open={defaultOpen}>
      <summary className="flex list-none items-center justify-between gap-2 rounded-lg px-3.5 py-2.5 text-[13px] font-medium text-ink-soft hover:bg-subtle">
        <span className="min-w-0">{summary}</span>
        <ChevronDown size={15} className="shrink-0 text-muted-ink transition-transform group-open:rotate-180" />
      </summary>
      <div className="min-w-0 border-t px-3.5 py-3">{children}</div>
    </details>
  );
}

export function Stat({ label, value, hint, tone }: { label: ReactNode; value: ReactNode; hint?: ReactNode; tone?: "default" | "warning" }) {
  return (
    <div className="min-w-0">
      <p className="text-[13px] text-muted-ink">{label}</p>
      <p className={cn("mt-0.5 text-xl font-semibold tabular-nums", tone === "warning" ? "text-amber-700" : "text-ink")}>{value}</p>
      {hint ? <p className="text-xs text-muted-ink">{hint}</p> : null}
    </div>
  );
}

/** Segmented control for switching between a handful of views. */
export function Segmented<T extends string>({
  value,
  options,
  onChange,
  label,
  className,
}: {
  value: T;
  options: Array<{ value: T; label: ReactNode; count?: number }>;
  onChange: (value: T) => void;
  label: string;
  className?: string;
}) {
  return (
    <div className={cn("inline-flex max-w-full overflow-x-auto rounded-lg bg-subtle p-0.5 ring-1 ring-inset ring-line", className)} role="group" aria-label={label}>
      {options.map((option) => (
        <button
          aria-pressed={value === option.value}
          className={cn(
            "inline-flex h-8 shrink-0 items-center gap-1.5 rounded-md px-3 text-[13px] font-medium text-muted-ink transition-colors hover:text-ink",
            value === option.value && "bg-panel text-ink shadow-[var(--shadow-card)]",
          )}
          key={option.value}
          onClick={() => onChange(option.value)}
          type="button"
        >
          {option.label}
          {option.count != null ? <span className="tabular-nums text-xs text-muted-ink">{option.count}</span> : null}
        </button>
      ))}
    </div>
  );
}
