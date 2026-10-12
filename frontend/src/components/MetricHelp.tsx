import { CircleHelp } from "lucide-react";

export function MetricHelp({ label, description }: { label: string; description: string }) {
  return (
    <details className="min-w-0">
      <summary title={description} className="flex cursor-help list-none items-center gap-1 rounded text-xs text-muted-ink focus-visible:outline-2 focus-visible:outline-brand [&::-webkit-details-marker]:hidden">
        <span>{label}</span><CircleHelp size={13} className="shrink-0" aria-hidden="true" />
      </summary>
      <p className="my-1.5 rounded-md border bg-subtle p-2 text-xs leading-5 text-ink-soft">{description}</p>
    </details>
  );
}
