import { cva, type VariantProps } from "class-variance-authority";
import type { HTMLAttributes } from "react";

import { cn } from "../../lib/utils";

const badgeVariants = cva("inline-flex items-center rounded-full px-2 py-0.5 text-xs font-medium", {
  variants: {
    tone: {
      neutral: "bg-slate-100 text-slate-700",
      success: "bg-emerald-100 text-emerald-800",
      info: "bg-sky-100 text-sky-800",
      warning: "bg-amber-100 text-amber-900",
      danger: "bg-red-100 text-red-800",
      brand: "bg-brand-soft text-brand",
    },
  },
  defaultVariants: { tone: "neutral" },
});

type BadgeProps = HTMLAttributes<HTMLSpanElement> & VariantProps<typeof badgeVariants>;

export function Badge({ className, tone, ...props }: BadgeProps) {
  return <span className={cn(badgeVariants({ tone }), className)} {...props} />;
}
