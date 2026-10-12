import type { HTMLAttributes, ReactNode } from "react";

import { cn } from "../../lib/utils";

export function Card({ className, ...props }: HTMLAttributes<HTMLDivElement>) {
  return <section className={cn("min-w-0 rounded-xl border bg-panel shadow-[var(--shadow-card)]", className)} {...props} />;
}

export function CardHeader({ className, ...props }: HTMLAttributes<HTMLDivElement>) {
  return <div className={cn("flex min-w-0 items-start justify-between gap-4 px-5 pt-4", className)} {...props} />;
}

export function CardContent({ className, ...props }: HTMLAttributes<HTMLDivElement>) {
  return <div className={cn("min-w-0 p-5", className)} {...props} />;
}

/** Title block used at the top of cards: small title, optional description. */
export function CardTitle({ title, description, icon }: { title: ReactNode; description?: ReactNode; icon?: ReactNode }) {
  return (
    <div className="flex min-w-0 items-start gap-2.5">
      {icon ? <span className="mt-0.5 shrink-0 text-muted-ink">{icon}</span> : null}
      <div className="min-w-0">
        <h2 className="text-[15px] font-semibold leading-6 text-ink">{title}</h2>
        {description ? <p className="mt-0.5 text-[13px] leading-5 text-muted-ink">{description}</p> : null}
      </div>
    </div>
  );
}
