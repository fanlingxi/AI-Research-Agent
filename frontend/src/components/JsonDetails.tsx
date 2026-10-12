export function JsonDetails({ value }: { value: Record<string, unknown> }) {
  const fields = Object.keys(value).length;
  return (
    <details className="mt-2 min-w-0 rounded-lg border bg-subtle/50" data-testid="json-details">
      <summary className="rounded-lg px-3 py-1.5 text-xs text-muted-ink hover:text-ink">原始数据 · {fields} 个字段</summary>
      <pre tabIndex={0} aria-label="原始数据" className="max-h-72 max-w-full overflow-auto border-t p-3 text-xs leading-5 text-ink-soft">{JSON.stringify(value, null, 2)}</pre>
    </details>
  );
}
