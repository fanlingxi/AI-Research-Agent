import { cva, type VariantProps } from "class-variance-authority";
import type { HTMLAttributes } from "react";

import { cn } from "../../lib/utils";

const badgeVariants = cva(
  "inline-flex shrink-0 items-center gap-1 whitespace-nowrap rounded-md px-1.5 py-0.5 text-xs font-medium leading-4",
  {
    variants: {
      tone: {
        neutral: "bg-subtle text-ink-soft ring-1 ring-inset ring-line",
        success: "bg-success-soft text-emerald-800",
        info: "bg-info-soft text-sky-800",
        warning: "bg-warning-soft text-amber-900",
        danger: "bg-danger-soft text-red-800",
        brand: "bg-brand-soft text-brand-strong",
      },
    },
    defaultVariants: { tone: "neutral" },
  },
);

export type BadgeTone = NonNullable<VariantProps<typeof badgeVariants>["tone"]>;
type BadgeProps = HTMLAttributes<HTMLSpanElement> & VariantProps<typeof badgeVariants>;

export function Badge({ className, tone, ...props }: BadgeProps) {
  return <span className={cn(badgeVariants({ tone }), className)} {...props} />;
}
