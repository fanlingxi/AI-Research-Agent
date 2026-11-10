import json

from app.knowledge.extractor import SchemaKnowledgeExtractor, _sample_chunks
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


def test_long_document_sampling_covers_opening_middle_and_conclusion() -> None:
    chunks = [
        DocumentChunk(
            id=f"paper:long:chunk:{index}",
            paper_id="paper:long",
            title="Long Paper",
            text=f"Chunk {index}",
            chunk_index=index,
            token_count=4,
            source_tier="primary_fulltext",
            metadata={"page_start": index + 1, "page_end": index + 1},
        )
        for index in range(100)
    ]

    selected = _sample_chunks(chunks)

    assert len(selected) == 7
    assert [chunk.chunk_index for chunk in selected[:2]] == [0, 1]
    assert [chunk.chunk_index for chunk in selected[-2:]] == [98, 99]
    assert {chunk.chunk_index for chunk in selected[2:-2]} == {25, 49, 73}


class QuotingLLM:
    provider_name = "openai"

    def __init__(self, chunk_id: str, quote: str) -> None:
        self.chunk_id, self.quote = chunk_id, quote

    def invoke(self, prompt: str, system_prompt: str | None = None) -> str:
        evidence = {"paper_id": "p", "chunk_id": self.chunk_id, "page_start": 1,
                    "page_end": 1, "quote": self.quote}
        return json.dumps({
            "reading": {
                "research_problem": "研究检索增强生成如何减少大模型幻觉。",
                "core_contributions": ["比较检索增强与直接生成。"],
                "method_summary": "先检索相关段落，再基于段落生成回答。",
                "key_results": ["检索增强减少了幻觉。"],
                "limitations": ["依赖检索质量。"],
                "evidence": evidence,
            },
            "entities": [], "relations": [],
        }, ensure_ascii=False)


def _chunks(paper: PaperMetadata) -> list[DocumentChunk]:
    texts = ["本文首先介绍研究背景与相关工作，并说明实验设置。",
             "实验表明，检索 增强\n生成（RAG）能够减少幻觉问题，并提升回答的事实性。"]
    return [DocumentChunk(id=f"c{i}", paper_id=paper.id, title=paper.title, text=text,
                          chunk_index=i, token_count=20, source_tier="primary_fulltext",
                          metadata={"page_start": i + 1, "page_end": i + 1})
            for i, text in enumerate(texts)]


def test_grounding_relocates_layout_noisy_quote_to_exact_source_span() -> None:
    paper = PaperMetadata(id="p", title="RAG", source="pdf")
    chunks = _chunks(paper)
    # Wrong chunk, no CJK spaces and half-width brackets: still the source's words in order.
    llm = QuotingLLM("c0", "检索增强生成(RAG)能够减少幻觉问题，并提升回答的事实性")

    result = SchemaKnowledgeExtractor(llm).extract(paper, chunks)

    evidence = result.payload.reading.evidence
    assert (evidence.chunk_id, evidence.page_start) == ("c1", 2)
    assert evidence.quote == "检索 增强 生成（RAG）能够减少幻觉问题，并提升回答的事实性"
    assert not result.unlocated_evidence


def test_grounding_flags_unlocated_quote_instead_of_passing_it_off_as_source() -> None:
    paper = PaperMetadata(id="p", title="RAG", source="pdf")
    chunks = _chunks(paper)
    llm = QuotingLLM("c1", "检索增强生成显著降低了所有模型的幻觉比例")

    result = SchemaKnowledgeExtractor(llm).extract(paper, chunks)

    evidence = result.payload.reading.evidence
    assert evidence.chunk_id == "c1"
    assert evidence.quote.startswith("实验表明")
    assert result.unlocated_evidence == {("c1", evidence.quote)}


def _entity(name: str, chunk: DocumentChunk) -> dict:
    return {"name": name, "type": "Method", "summary": f"{name} 是该论文正文中使用的一种方法。",
            "aliases": [], "confidence": 0.8,
            "evidence": {"paper_id": chunk.paper_id, "chunk_id": chunk.id, "page_start": 1,
                         "page_end": 1, "quote": chunk.text}}


class FullTextLLM:
    """First call answers the sampled prompt; later calls answer one body batch each."""

    provider_name = "openai"

    def __init__(self, chunks: list[DocumentChunk], *, broken_batch: int | None = None) -> None:
        self.chunks, self.broken_batch, self.prompts = chunks, broken_batch, []

    def invoke(self, prompt: str, system_prompt: str | None = None) -> str:
        if "JSON 修复器" in (system_prompt or ""):
            return "still not json"
        self.prompts.append(prompt)
        first = self.chunks[0]
        if len(self.prompts) == 1:
            return json.dumps({
                "reading": {
                    "research_problem": "研究检索增强生成如何减少大模型幻觉。",
                    "core_contributions": ["比较检索增强与直接生成。"],
                    "method_summary": "先检索相关段落，再基于段落生成回答。",
                    "evidence": _entity("x", first)["evidence"],
                },
                "entities": [_entity("方法 0", first)], "relations": [],
            }, ensure_ascii=False)
        if self.broken_batch == len(self.prompts) - 1:
            return "not json"
        seen = [chunk for chunk in self.chunks if f"[chunk_id={chunk.id};" in prompt]
        slim = {"chunk_id": seen[0].id, "quote": seen[0].text}  # no paper_id / pages
        return json.dumps({
            # "方法 0" repeats an entity from the first call and must not be duplicated.
            "entities": [_entity("方法  0", seen[0]),
                         {**_entity(f"方法 {seen[0].chunk_index}", seen[0]), "evidence": slim},
                         {"name": "缺字段的条目", "type": "Method"}],
            "relations": [{"source_name": f"方法 {seen[0].chunk_index}", "target_name": "方法 0",
                           "type": "USES", "summary": "该方法在实验中使用了方法 0 作为组件。",
                           "confidence": 0.7, "evidence": _entity("x", seen[0])["evidence"]}],
        }, ensure_ascii=False)


def _long_paper(count: int = 30) -> tuple[PaperMetadata, list[DocumentChunk]]:
    paper = PaperMetadata(id="p", title="Long", source="pdf")
    return paper, [
        DocumentChunk(id=f"c{i}", paper_id="p", title="Long", chunk_index=i, token_count=900,
                      text=f"第 {i} 段正文讨论了方法 {i} 的设计与实验结果。" + "补充说明。" * 180,
                      source_tier="primary_fulltext", metadata={"page_start": 1, "page_end": 1})
        for i in range(count)
    ]


def test_fulltext_mode_reads_every_chunk_within_the_call_budget_and_merges_by_name() -> None:
    paper, chunks = _long_paper()
    llm = FullTextLLM(chunks)

    result = SchemaKnowledgeExtractor(llm, mode="fulltext-v1", max_calls=4).extract(paper, chunks)

    assert len(llm.prompts) == 4
    sampled = {chunk.id for chunk in _sample_chunks(chunks)}
    for chunk in chunks:
        shown = sum(f"[chunk_id={chunk.id};" in prompt for prompt in llm.prompts)
        assert shown == 1, chunk.id
        assert (f"[chunk_id={chunk.id};" in llm.prompts[0]) == (chunk.id in sampled)
    names = [entity.name for entity in result.payload.entities]
    assert len(names) == 4 and names.count("方法 0") == 1
    assert len(result.payload.relations) == 3
    assert "- 方法 0（Method）" in llm.prompts[1]
    assert result.stats == {"mode": "fulltext-v1", "chunk_count": 30, "supplement_batches": 3,
                            "failed_batches": 0, "dropped_items": 3, "uncovered_chunks": 0}
    assert all(entity.evidence.paper_id == "p" for entity in result.payload.entities)
    assert not result.unlocated_evidence


def test_fulltext_mode_keeps_other_batches_when_one_batch_is_unusable() -> None:
    paper, chunks = _long_paper()
    llm = FullTextLLM(chunks, broken_batch=2)

    result = SchemaKnowledgeExtractor(llm, mode="fulltext-v1", max_calls=4).extract(paper, chunks)

    assert result.stats["failed_batches"] == 1
    assert len(result.payload.entities) == 3


def test_default_mode_still_makes_one_sampled_call() -> None:
    paper, chunks = _long_paper()
    llm = FullTextLLM(chunks)

    result = SchemaKnowledgeExtractor(llm).extract(paper, chunks)

    assert len(llm.prompts) == 1 and result.stats == {}


def test_paper_longer_than_the_call_budget_gets_larger_batches_not_unread_chunks() -> None:
    paper, chunks = _long_paper(60)
    llm = FullTextLLM(chunks)

    result = SchemaKnowledgeExtractor(llm, mode="fulltext-v1", max_calls=3).extract(paper, chunks)

    assert len(llm.prompts) == 3
    assert all(sum(f"[chunk_id={chunk.id};" in prompt for prompt in llm.prompts) == 1
               for chunk in chunks)
    assert result.stats["supplement_batches"] == 2 and result.stats["uncovered_chunks"] == 0
