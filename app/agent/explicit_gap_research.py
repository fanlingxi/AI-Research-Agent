"""Opt-in research_v12: explicit gaps without changing frozen report contracts."""

from pydantic import Field, ValidationError

from app.agent.structured_research import (
    DirectAnswerPart,
    DirectReport,
    NumericSupportResearchWorkflow,
    report_error_diagnostic,
)


class ExplicitGapAnswerPart(DirectAnswerPart):
    gap: str = Field(
        max_length=600,
        description=(
            'Required on every part, even with findings. Use "" only when fully answered; '
            "otherwise describe the specific missing requested fact. When findings=[], "
            "gap must be nonempty. Never omit this key or use null."
        ),
    )


class ExplicitGapReport(DirectReport):
    answer_parts: list[ExplicitGapAnswerPart] = Field(min_length=1, max_length=8)


MISSING_GAP_DIAGNOSTIC = (
    'Invalid schema: every answer_parts item must include gap; use "" only when '
    "that part is fully answered"
)

# Complete report objects, not fragments. IDs and question spans are illustrative;
# actual responses must use the immutable snapshot and original task instead.
GAP_PROMPT_SUFFIX = '''
Explicit gap contract: every answer_parts item MUST explicitly contain all three
keys: question_part, findings, gap. Findings do not imply the whole part is answered.
Before returning JSON, check these keys on EVERY item; never omit gap or use null.
Use gap="" only when that part is fully answered. Otherwise state the missing
requested fact, including when findings is nonempty. With findings=[], gap must
describe the missing evidence. Do not output answer_status or limitations.
Three complete JSON shapes follow. Example IDs, facts and question spans are
illustrative only; replace them with allowed evidence and verbatim original task spans.
Fully answered:
{
  "title": "Supported answer",
  "answer_parts": [
    {
      "question_part": "What does the source support?",
      "findings": [
        {
          "claim_bundle_id": "bundle-id",
          "assertion": "The source supports the requested fact.",
          "evidence_ids": [
            "evidence-id"
          ],
          "scope": "",
          "support": [
            {
              "evidence_id": "evidence-id",
              "quote": "The source supports the requested fact."
            }
          ]
        }
      ],
      "gap": ""
    }
  ]
}
Partially answered, despite nonempty findings:
{
  "title": "Partial answer",
  "answer_parts": [
    {
      "question_part": "What does the source support?",
      "findings": [
        {
          "claim_bundle_id": "bundle-id",
          "assertion": "The source supports the requested fact.",
          "evidence_ids": [
            "evidence-id"
          ],
          "scope": "",
          "support": [
            {
              "evidence_id": "evidence-id",
              "quote": "The source supports the requested fact."
            }
          ]
        }
      ],
      "gap": "The requested comparison is unsupported by the supplied evidence."
    }
  ]
}
Cannot answer:
{
  "title": "Missing evidence",
  "answer_parts": [
    {
      "question_part": "What does the source support?",
      "findings": [],
      "gap": "The supplied evidence does not support the requested fact."
    }
  ]
}
Final key check: every answer_parts item explicitly includes question_part, findings,
and gap, including fully answered items with gap="". Return the complete JSON object.
'''


class ExplicitGapResearchWorkflow(NumericSupportResearchWorkflow):
    """structured-v11; retain numeric filtering, validation and two durable slots."""

    report_model = ExplicitGapReport

    def _report_prompt(self, state, package):
        return super()._report_prompt(state, package) + GAP_PROMPT_SUFFIX

    @staticmethod
    def _report_error_diagnostic(exc):
        if isinstance(exc, ValidationError) and any(
            error["type"] == "missing"
            and len(error["loc"]) == 3
            and error["loc"][0] == "answer_parts"
            and type(error["loc"][1]) is int
            and error["loc"][2] == "gap"
            for error in exc.errors(include_input=False, include_context=False, include_url=False)
        ):
            return MISSING_GAP_DIAGNOSTIC
        return report_error_diagnostic(exc)
