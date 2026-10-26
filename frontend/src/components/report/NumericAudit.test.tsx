import { render, screen } from "@testing-library/react";
import { expect, it } from "vitest";

import { type AgentOutput } from "../../lib/api";
import { NumericAuditDisclosure, numericOmissions } from "./NumericAudit";

const output = (draft: Record<string, unknown>) => ({
  id: "output", run_id: "run", output_type: "research_report", rendered_text: "", validation: {}, created_at: "",
  output_sha256: "sha", structured: { draft },
}) as AgentOutput;

it("lists findings omitted by the numeric audit with their unsupported numbers", () => {
  render(<NumericAuditDisclosure output={output({
    answer_status: "insufficient_evidence",
    numeric_audit: { rule_version: "numeric-support-v1", omitted: [{
      question_part: "CoVe 比基线高多少", assertion: "基线 0.12，CoVe 0.22，高 10 个百分点。",
      unsupported_numbers: [{ text: "0.12", unit: "" }, { text: "0.22", unit: "" }, { text: "10", unit: "points" }, { text: "0.12", unit: "" }],
    }, { assertion: "准确率 85.1%。", unsupported_numbers: [{ text: "85.1", unit: "percent" }] }] },
  })} />);
  expect(screen.getByText("数字核验 · 省略 2 条结论")).toBeInTheDocument();
  expect(screen.getByText("基线 0.12，CoVe 0.22，高 10 个百分点。")).toBeInTheDocument();
  expect(screen.getByText("0.12、0.22、10")).toBeInTheDocument();
  expect(screen.getByText("85.1%")).toBeInTheDocument();
});

it("renders nothing for reports without a numeric audit", () => {
  expect(numericOmissions(output({ answer_status: "complete" }))).toEqual([]);
  const { container } = render(<NumericAuditDisclosure output={output({ numeric_audit: { omitted: [] } })} />);
  expect(container).toBeEmptyDOMElement();
});
