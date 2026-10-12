import { cva, type VariantProps } from "class-variance-authority";
import type { ButtonHTMLAttributes } from "react";

import { cn } from "../../lib/utils";

export const buttonVariants = cva(
  "inline-flex shrink-0 items-center justify-center gap-1.5 whitespace-nowrap rounded-lg text-sm font-medium transition-colors focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand disabled:pointer-events-none disabled:opacity-50",
  {
    variants: {
      variant: {
        default: "bg-brand text-on-brand shadow-[var(--shadow-card)] hover:bg-brand-strong",
        secondary: "bg-brand-soft text-brand-strong hover:bg-brand-hover",
        outline: "border border-line-strong bg-panel text-ink-soft hover:bg-subtle hover:text-ink",
        ghost: "text-ink-soft hover:bg-subtle hover:text-ink",
        danger: "bg-danger-solid text-white hover:bg-danger-hover",
        "danger-outline": "border border-red-200 bg-panel text-red-700 hover:bg-danger-soft",
        link: "h-auto px-0 py-0 text-brand underline-offset-4 hover:underline",
      },
      size: {
        default: "h-9 px-3.5",
        sm: "h-8 px-2.5 text-[13px]",
        lg: "h-11 px-5 text-[15px]",
        icon: "size-9 px-0",
        "icon-sm": "size-8 px-0",
      },
    },
    defaultVariants: { variant: "default", size: "default" },
  },
);

type ButtonProps = ButtonHTMLAttributes<HTMLButtonElement> & VariantProps<typeof buttonVariants>;

export function Button({ className, variant, size, type = "button", ...props }: ButtonProps) {
  return <button className={cn(buttonVariants({ variant, size }), className)} type={type} {...props} />;
}
