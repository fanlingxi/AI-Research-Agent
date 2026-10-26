import { type AgentOutput } from "../../lib/api";
import { Disclosure } from "../ui/page";

export interface NumericOmission {
  questionPart: string;
  assertion: string;
  numbers: string[];
}

/** research_v11 omits findings whose numbers are absent from their cited quotes; older outputs have no audit. */
export function numericOmissions(output?: AgentOutput): NumericOmission[] {
  const audit = (output?.structured.draft as { numeric_audit?: { omitted?: unknown } } | undefined)?.numeric_audit;
  if (!Array.isArray(audit?.omitted)) return [];
  return audit.omitted.flatMap((item) => {
    const entry = item as { question_part?: unknown; assertion?: unknown; unsupported_numbers?: unknown };
    if (typeof entry.assertion !== "string") return [];
    const numbers = Array.isArray(entry.unsupported_numbers)
      ? entry.unsupported_numbers.flatMap((n) => {
        const value = n as { text?: unknown; unit?: unknown };
        return typeof value.text === "string" ? [value.text + (value.unit === "percent" ? "%" : "")] : [];
      })
      : [];
    return [{ questionPart: typeof entry.question_part === "string" ? entry.question_part : "", assertion: entry.assertion, numbers: [...new Set(numbers)] }];
  });
}

export function NumericAuditDisclosure({ output }: { output?: AgentOutput }) {
  const omitted = numericOmissions(output);
  if (!omitted.length) return null;
  return (
    <Disclosure summary={`数字核验 · 省略 ${omitted.length} 条结论`}>
      <div className="space-y-3 text-[13px] leading-6">
        <p className="text-muted-ink">这些结论里的数字不在所引原文片段中，无法核验，未写入正文。可通过下方“补充证据”审核含这些数字的原文后重新研究。</p>
        <ol className="space-y-3">
          {omitted.map((item, index) => (
            <li className="space-y-1 border-t pt-3 first:border-t-0 first:pt-0" key={index}>
              {item.questionPart ? <p className="text-xs text-muted-ink">{item.questionPart}</p> : null}
              <p className="text-ink-soft">{item.assertion}</p>
              {item.numbers.length ? (
                <p className="text-xs text-muted-ink">未获支持的数字：<span className="font-mono text-ink-soft">{item.numbers.join("、")}</span></p>
              ) : null}
            </li>
          ))}
        </ol>
      </div>
    </Disclosure>
  );
}
