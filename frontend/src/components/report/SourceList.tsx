import { cn } from "../../lib/utils";

export interface SourceListItem {
  id: string;
  title: string;
  page: string;
  excerpt?: string;
}

/** Numbered list of cited sources; numbers match the citation chips in the report body. */
export function SourceList({ items, activeId, onSelect, emptyText = "这份报告没有可点击的引用。" }: {
  items: SourceListItem[];
  activeId?: string | null;
  onSelect: (id: string) => void;
  emptyText?: string;
}) {
  if (!items.length) return <p className="text-[13px] text-muted-ink">{emptyText}</p>;
  return (
    <ol className="space-y-1">
      {items.map((item, index) => (
        <li key={item.id}>
          <button
            className={cn(
              "flex w-full gap-2.5 rounded-lg px-2 py-2 text-left transition-colors hover:bg-subtle",
              activeId === item.id && "bg-brand-tint ring-1 ring-brand/20",
            )}
            onClick={() => onSelect(item.id)}
            type="button"
          >
            <span className={cn(
              "mt-0.5 grid h-[18px] min-w-[18px] place-items-center rounded-[5px] px-1 text-[11px] font-semibold tabular-nums",
              activeId === item.id ? "bg-brand text-white" : "bg-brand-soft text-brand-strong",
            )}>{index + 1}</span>
            <span className="min-w-0">
              <span className="line-clamp-2 text-[13px] font-medium leading-5 text-ink">{item.title}</span>
              <span className="mt-0.5 block text-xs text-muted-ink">{item.page}</span>
              {item.excerpt ? <span className="mt-1 line-clamp-2 text-xs leading-5 text-muted-ink">{item.excerpt}</span> : null}
            </span>
          </button>
        </li>
      ))}
    </ol>
  );
}
