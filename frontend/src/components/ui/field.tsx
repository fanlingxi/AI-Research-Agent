import type { InputHTMLAttributes, ReactNode, SelectHTMLAttributes, TextareaHTMLAttributes } from "react";

import { cn } from "../../lib/utils";

export const controlClass =
  "w-full min-w-0 rounded-lg border border-line-strong bg-panel px-3 text-sm text-ink outline-none transition-colors placeholder:text-muted-ink/70 hover:border-muted-ink focus:border-brand focus:ring-3 focus:ring-brand/12 disabled:cursor-not-allowed disabled:bg-subtle disabled:opacity-70";

export function Input({ className, ...props }: InputHTMLAttributes<HTMLInputElement>) {
  return <input className={cn(controlClass, "h-9", className)} {...props} />;
}

export function Textarea({ className, ...props }: TextareaHTMLAttributes<HTMLTextAreaElement>) {
  return <textarea className={cn(controlClass, "py-2 leading-6", className)} {...props} />;
}

export function Select({ className, ...props }: SelectHTMLAttributes<HTMLSelectElement>) {
  return <select className={cn(controlClass, "h-9 pr-8", className)} {...props} />;
}

/** Label wrapping its control so accessible names come from the visible text. */
export function Field({ label, hint, children, className }: { label: ReactNode; hint?: ReactNode; children: ReactNode; className?: string }) {
  return (
    <label className={cn("block min-w-0 text-[13px] font-medium text-ink-soft", className)}>
      {label}
      <div className="mt-1.5">{children}</div>
      {hint ? <span className="mt-1.5 block text-xs font-normal leading-5 text-muted-ink">{hint}</span> : null}
    </label>
  );
}
