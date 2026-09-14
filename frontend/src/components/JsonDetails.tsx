export function JsonDetails({ value }: { value: Record<string, unknown> }) {
  const fields = Object.keys(value).length;
  return (
    <details className="mt-3 min-w-0 rounded-lg border bg-slate-50" data-testid="json-details">
      <summary className="cursor-pointer rounded-lg px-3 py-2 text-xs font-medium text-brand focus-visible:outline-2 focus-visible:outline-brand">
        查看完整 JSON · {fields} 个字段
      </summary>
      <pre tabIndex={0} aria-label="完整 JSON 内容" className="max-h-72 max-w-full overflow-auto border-t p-3 text-xs leading-5 text-slate-600">{JSON.stringify(value, null, 2)}</pre>
    </details>
  );
}
