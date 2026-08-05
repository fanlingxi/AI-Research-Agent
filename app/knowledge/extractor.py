from __future__ import annotations

import json
import re
from dataclasses import dataclass

from pydantic import ValidationError

from app.knowledge.schemas import EvidenceSpan, KnowledgeExtraction
from app.llms.provider import LLMClient, MockLLMClient
from app.schemas.documents import DocumentChunk, PaperMetadata

EXTRACTION_SYSTEM_PROMPT = """你是严谨的科研知识工程师。你的任务是把一篇论文转换为可审阅的候选知识，
而不是罗列关键词。所有自然语言字段必须使用中文。

只抽取对理解研究有价值的论文、概念、方法、任务、数据集、指标或发现。禁止抽取作者姓名、会议模板词、
章节标题、参考文献条目、泛化词（例如 Association、Introduction、Proceedings）。

实体摘要只能描述该论文中的用法，不要把模型常识写成跨论文定义。若正文足以支持，请补充：
paper_context（这篇论文如何使用该术语）、role（它在研究中的作用）、conditions（适用条件或局限）和
sense_qualifier（不超过 16 字的词义限定语）；没有明确证据时返回空字符串。
没有明确证据时返回空字符串。

关系只能使用：PRESENTS、ADDRESSES、USES、EVALUATES、IMPROVES、COMPARES_WITH、APPLIES_TO、
HAS_LIMITATION、SUPPORTS。
每个实体和关系必须给出一条提供的原文证据。evidence.chunk_id 必须精确使用上下文中的 chunk_id；
evidence.quote 必须是该切片中连续出现的原文短句，且不要超过 280 个字符。不要编造实验结果。

只返回符合用户给定 JSON schema 的对象，不要使用 Markdown 代码块。"""

REPAIR_SYSTEM_PROMPT = """你是 JSON 修复器。仅返回修正后的合法 JSON；保留有证据支撑的事实，
删除无法满足 schema 或证据要求的项目。"""

NOISE_NAMES = {
    "association",
    "inproceedings",
    "introduction",
    "abstract",
    "appendix",
    "references",
    "arxiv",
    "proceedings",
    "paper",
    "method",
}


class LiveLLMRequiredError(RuntimeError):
    """Knowledge publication must never silently fall back to synthetic mock output."""


@dataclass
class ExtractionResult:
    payload: KnowledgeExtraction
    used_repair: bool = False


class SchemaKnowledgeExtractor:
    """LLM extraction with Pydantic validation, one repair attempt, and source grounding."""

    def __init__(self, llm: LLMClient) -> None:
        self.llm = llm

    def extract(
        self,
        paper: PaperMetadata,
        chunks: list[DocumentChunk],
    ) -> ExtractionResult:
        if (
            isinstance(self.llm, MockLLMClient)
            or getattr(self.llm, "provider_name", "mock") == "mock"
        ):
            raise LiveLLMRequiredError("知识入库需要配置真实 LLM，mock 仅用于测试。")
        if not chunks:
            raise ValueError("论文没有可用于知识抽取的正文切片。")

        prompt = self._build_prompt(paper, chunks)
        raw = self.llm.invoke(prompt, system_prompt=EXTRACTION_SYSTEM_PROMPT)
        try:
            payload = self._parse(raw)
            used_repair = False
        except (ValidationError, ValueError, json.JSONDecodeError) as exc:
            repair_prompt = self._repair_prompt(raw, str(exc))
            repaired = self.llm.invoke(repair_prompt, system_prompt=REPAIR_SYSTEM_PROMPT)
            payload = self._parse(repaired)
            used_repair = True

        return ExtractionResult(payload=self._ground(payload, chunks), used_repair=used_repair)

    def _build_prompt(self, paper: PaperMetadata, chunks: list[DocumentChunk]) -> str:
        selected = _sample_chunks(chunks)
        context = "\n\n".join(
            "\n".join(
                [
                    f"[chunk_id={chunk.id}; page={chunk.metadata.get('page_start', 1)}]",
                    chunk.text[:1400],
                ]
            )
            for chunk in selected
        )
        return "\n\n".join(
            [
                f"论文标题：{paper.title}",
                "请返回以下 JSON 结构：",
                """{
  "reading": {
    "research_problem": "string",
    "core_contributions": ["string"],
    "method_summary": "string",
    "key_results": ["string"],
    "limitations": ["string"],
    "evidence": {
      "paper_id": "string", "chunk_id": "string", "page_start": 1,
      "page_end": 1, "quote": "string"
    }
  },
  "entities": [{
    "name": "string", "type": "Concept|Method|Task|Dataset|Metric|Finding",
    "summary": "string", "aliases": ["string"], "sense_qualifier": "string",
    "paper_context": "string",
    "role": "string", "conditions": "string", "confidence": 0.0,
    "evidence": {"paper_id": "string", "chunk_id": "string", "page_start": 1,
      "page_end": 1, "quote": "string"}
  }],
  "relations": [{
    "source_name": "string", "target_name": "string",
    "type": "受控关系类型（见系统说明）",
    "summary": "string", "confidence": 0.0,
    "evidence": {"paper_id": "string", "chunk_id": "string", "page_start": 1,
      "page_end": 1, "quote": "string"}
  }]
}""",
                "可用正文证据：\n" + context,
            ]
        )

    def _repair_prompt(self, raw: str, error: str) -> str:
        return "\n\n".join(
            [
                "以下输出未通过 schema 校验。请仅返回修复后的 JSON。",
                "校验错误：" + error[:1200],
                "原始输出：\n" + raw[:12000],
            ]
        )

    def _parse(self, raw: str) -> KnowledgeExtraction:
        cleaned = raw.strip()
        if cleaned.startswith("```"):
            cleaned = re.sub(r"^```(?:json)?", "", cleaned).strip()
            cleaned = re.sub(r"```$", "", cleaned).strip()
        match = re.search(r"\{.*\}", cleaned, flags=re.DOTALL)
        if match:
            cleaned = match.group(0)
        return KnowledgeExtraction.model_validate_json(cleaned)

    def _ground(
        self, payload: KnowledgeExtraction, chunks: list[DocumentChunk]
    ) -> KnowledgeExtraction:
        by_id = {chunk.id: chunk for chunk in chunks}

        def ground(evidence: EvidenceSpan) -> EvidenceSpan:
            chunk = by_id.get(evidence.chunk_id) or chunks[0]
            page = int(chunk.metadata.get("page_start", 1) or 1)
            quote = evidence.quote.strip()
            if not _is_quote_in_chunk(quote, chunk.text):
                quote = _evidence_quote(chunk.text)
            return EvidenceSpan(
                paper_id=chunk.paper_id,
                chunk_id=chunk.id,
                page_start=page,
                page_end=int(chunk.metadata.get("page_end", page) or page),
                quote=quote,
            )

        entities = [
            entity.model_copy(update={"evidence": ground(entity.evidence)})
            for entity in payload.entities
            if _meaningful_name(entity.name)
        ]
        known_names = {entity.name.casefold() for entity in entities}
        relations = [
            relation.model_copy(update={"evidence": ground(relation.evidence)})
            for relation in payload.relations
            if relation.source_name.casefold() != relation.target_name.casefold()
            and relation.source_name.casefold() in known_names
            and relation.target_name.casefold() in known_names
        ]
        return payload.model_copy(
            update={
                "reading": payload.reading.model_copy(
                    update={"evidence": ground(payload.reading.evidence)}
                ),
                "entities": entities,
                "relations": relations,
            }
        )


def _sample_chunks(chunks: list[DocumentChunk], limit: int = 7) -> list[DocumentChunk]:
    if len(chunks) <= limit:
        return chunks
    first_count = max(1, limit - 2)
    return chunks[:first_count] + chunks[-2:]


def _meaningful_name(value: str) -> bool:
    normalized = re.sub(r"\s+", " ", value.strip()).casefold()
    return len(normalized) >= 3 and normalized not in NOISE_NAMES and not normalized.isdigit()


def _is_quote_in_chunk(quote: str, chunk_text: str) -> bool:
    if len(quote) < 16:
        return False
    normalized_quote = re.sub(r"\s+", " ", quote).strip().casefold()
    normalized_chunk = re.sub(r"\s+", " ", chunk_text).casefold()
    return normalized_quote in normalized_chunk


def _evidence_quote(text: str) -> str:
    normalized = re.sub(r"\s+", " ", text).strip()
    return normalized[:280] or "正文未提取到可展示的证据。"
