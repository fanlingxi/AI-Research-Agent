import json

from app.knowledge.extractor import SchemaKnowledgeExtractor
from app.schemas.documents import DocumentChunk, PaperMetadata


class RepairingLLM:
    provider_name = "openai"

    def __init__(self) -> None:
        self.calls = 0

    def invoke(self, prompt: str, system_prompt: str | None = None) -> str:
        self.calls += 1
        if self.calls == 1:
            return "this is not valid json"
        return json.dumps(
            {
                "reading": {
                    "research_problem": "研究如何让语言模型可靠地调用外部工具完成任务。",
                    "core_contributions": ["提出了基于工具调用的训练机制。"],
                    "method_summary": "模型学习在生成过程中选择工具并利用返回结果继续推理。",
                    "key_results": ["在工具使用任务上获得更好的完成率。"],
                    "limitations": ["依赖外部工具的可用性与调用成本。"],
                    "evidence": {
                        "paper_id": "wrong",
                        "chunk_id": "unknown",
                        "page_start": 99,
                        "page_end": 99,
                        "quote": "not found in source",
                    },
                },
                "entities": [
                    {
                        "name": "工具增强语言模型",
                        "type": "Method",
                        "summary": "让语言模型在推理过程中调用外部 API 并使用返回结果的方法。",
                        "aliases": ["Tool-augmented LLM"],
                        "confidence": 0.86,
                        "evidence": {
                            "paper_id": "wrong",
                            "chunk_id": "unknown",
                            "page_start": 99,
                            "page_end": 99,
                            "quote": "not found in source",
                        },
                    }
                ],
                "relations": [],
            },
            ensure_ascii=False,
        )


def test_schema_extractor_repairs_once_and_normalizes_evidence() -> None:
    paper = PaperMetadata(id="paper:test", title="Tool Test", source="pdf")
    chunk = DocumentChunk(
        id="paper:test:page:2:chunk:0",
        paper_id=paper.id,
        title=paper.title,
        text="Toolformer teaches language models to decide which external tools should be called.",
        chunk_index=0,
        token_count=12,
        source_tier="primary_fulltext",
        metadata={"page_start": 2, "page_end": 2},
    )
    llm = RepairingLLM()

    result = SchemaKnowledgeExtractor(llm).extract(paper, [chunk])

    assert result.used_repair
    assert llm.calls == 2
    assert result.payload.reading.evidence.chunk_id == chunk.id
    assert result.payload.reading.evidence.page_start == 2
    assert result.payload.reading.evidence.quote in chunk.text
