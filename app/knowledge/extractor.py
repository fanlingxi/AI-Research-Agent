from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from pydantic import ValidationError

from app.knowledge.schemas import (
    EvidenceSpan,
    ExtractedEntity,
    ExtractedRelation,
    KnowledgeExtraction,
)
from app.llms.provider import LLMClient, MockLLMClient
from app.retrieval.anchors import locate_quote
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

SUPPLEMENT_INSTRUCTION = """这是同一篇论文的后续正文。请只补充“已抽取实体”中没有的新实体，
以及这些正文明确支持的新关系。关系的 source_name 与 target_name 必须逐字使用“已抽取实体”
或本次新实体的 name；同一概念不要换名字重复抽取。
这些正文没有新增内容时返回空数组。evidence 只能引用下面给出的 chunk_id。
每一项的所有字段都必须填写，只返回以下 JSON 结构：
{
  "entities": [{
    "name": "string", "type": "Concept|Method|Task|Dataset|Metric|Finding",
    "summary": "string", "aliases": ["string"], "sense_qualifier": "string",
    "paper_context": "string", "role": "string", "conditions": "string", "confidence": 0.0,
    "evidence": {"chunk_id": "string", "quote": "string"}
  }],
  "relations": [{
    "source_name": "string", "target_name": "string",
    "type": "受控关系类型（见系统说明）",
    "summary": "string", "confidence": 0.0,
    "evidence": {"chunk_id": "string", "quote": "string"}
  }]
}"""

SAMPLED_MODE = "sampled-v1"
FULLTEXT_MODE = "fulltext-v1"
_SAMPLE_CHARS = 1400
_BATCH_CHARS = 10000

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
    # (chunk_id, quote) pairs whose model quote could not be located in any supplied chunk;
    # their quote is a chunk excerpt the reviewer must check against the source.
    unlocated_evidence: set[tuple[str, str]] = field(default_factory=set)
    # Empty for sampled-v1 so existing candidate metadata stays byte-identical.
    stats: dict[str, int | str] = field(default_factory=dict)


@dataclass
class SupplementExtraction:
    """Valid entities and relations from one later-body batch (no reading card)."""

    entities: list[ExtractedEntity] = field(default_factory=list)
    relations: list[ExtractedRelation] = field(default_factory=list)
    dropped: int = 0


class SchemaKnowledgeExtractor:
    """LLM extraction with Pydantic validation, one repair attempt, and source grounding.

    ``sampled-v1`` reads seven sampled chunks in one call.  ``fulltext-v1`` keeps that
    call for the reading card, then reads every remaining chunk in at most
    ``max_calls - 1`` further calls and merges the additions by name.
    """

    def __init__(self, llm: LLMClient, *, mode: str = SAMPLED_MODE, max_calls: int = 8) -> None:
        if mode not in (SAMPLED_MODE, FULLTEXT_MODE):
            raise ValueError(f"Unknown knowledge extraction mode: {mode}")
        if max_calls < 1:
            raise ValueError("max_calls must be positive")
        self.llm = llm
        self.mode = mode
        self.max_calls = max_calls

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
        payload, used_repair = self._invoke(prompt, self._parse)
        stats: dict[str, int | str] = {}
        if self.mode == FULLTEXT_MODE:
            payload, repaired, stats = self._supplement(paper, chunks, payload)
            used_repair = used_repair or repaired

        unlocated: set[tuple[str, str]] = set()
        grounded = self._ground(payload, chunks, unlocated)
        return ExtractionResult(
            payload=grounded, used_repair=used_repair, unlocated_evidence=unlocated,
            stats=stats,
        )

    def _invoke(self, prompt: str, parse):
        """One model call plus at most one JSON repair call."""
        raw = self.llm.invoke(prompt, system_prompt=EXTRACTION_SYSTEM_PROMPT)
        try:
            return parse(raw), False
        except (ValidationError, ValueError, json.JSONDecodeError) as exc:
            repair_prompt = self._repair_prompt(raw, str(exc))
            repaired = self.llm.invoke(repair_prompt, system_prompt=REPAIR_SYSTEM_PROMPT)
            return parse(repaired), True

    def _supplement(
        self, paper: PaperMetadata, chunks: list[DocumentChunk], payload: KnowledgeExtraction,
    ) -> tuple[KnowledgeExtraction, bool, dict[str, int | str]]:
        """Read the body the sampled call did not see and merge new items by name."""
        sampled = {chunk.id for chunk in _sample_chunks(chunks)}
        remaining = [
            chunk for chunk in chunks
            if chunk.id not in sampled or len(chunk.text) > _SAMPLE_CHARS
        ]
        batches = _batches(remaining, self.max_calls - 1)
        entities = list(payload.entities)
        relations = list(payload.relations)
        names = {_name_key(entity.name) for entity in entities}
        edges = {_edge_key(relation) for relation in relations}
        used_repair = False
        failed = dropped = 0
        for batch in batches:
            try:
                extra, repaired = self._invoke(
                    self._supplement_prompt(paper, batch, entities),
                    lambda raw: self._parse_supplement(raw, paper),
                )
            except (ValidationError, ValueError, json.JSONDecodeError):
                # One unusable batch must not discard the rest of the paper; the count is
                # kept in the candidate metadata so the gap is visible to the reviewer.
                failed += 1
                continue
            used_repair = used_repair or repaired
            dropped += extra.dropped
            for entity in extra.entities:
                if _name_key(entity.name) not in names:
                    names.add(_name_key(entity.name))
                    entities.append(entity)
            for relation in extra.relations:
                if _edge_key(relation) not in edges:
                    edges.add(_edge_key(relation))
                    relations.append(relation)
        stats: dict[str, int | str] = {
            "mode": self.mode,
            "chunk_count": len(chunks),
            "supplement_batches": len(batches),
            "failed_batches": failed,
            "dropped_items": dropped,
            "uncovered_chunks": 0 if batches else len(remaining),
        }
        # model_copy skips the per-call list bounds on purpose: they bound one response.
        return (
            payload.model_copy(update={"entities": entities, "relations": relations}),
            used_repair,
            stats,
        )

    def _supplement_prompt(
        self, paper: PaperMetadata, batch: list[DocumentChunk], entities: list[ExtractedEntity],
    ) -> str:
        known = "\n".join(f"- {entity.name}（{entity.type}）" for entity in entities) or "（无）"
        context = "\n\n".join(
            f"[chunk_id={chunk.id}; page={chunk.metadata.get('page_start', 1)}]\n{chunk.text}"
            for chunk in batch
        )
        return "\n\n".join(
            [
                f"论文标题：{paper.title}",
                SUPPLEMENT_INSTRUCTION,
                "已抽取实体：\n" + known,
                "可用正文证据：\n" + context,
            ]
        )

    def _build_prompt(self, paper: PaperMetadata, chunks: list[DocumentChunk]) -> str:
        selected = _sample_chunks(chunks)
        context = "\n\n".join(
            "\n".join(
                [
                    f"[chunk_id={chunk.id}; page={chunk.metadata.get('page_start', 1)}]",
                    chunk.text[:_SAMPLE_CHARS],
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

    def _parse_supplement(self, raw: str, paper: PaperMetadata) -> SupplementExtraction:
        """Validate a batch item by item: one malformed item must not void its batch.

        Only JSON that is not an object fails the batch (and earns the one repair call).
        """
        data = json.loads(_json_text(raw))
        if not isinstance(data, dict):
            raise ValueError("supplement response is not a JSON object")
        result = SupplementExtraction()
        for key, model, target, limit in (
            ("entities", ExtractedEntity, result.entities, 20),
            ("relations", ExtractedRelation, result.relations, 30),
        ):
            items = data.get(key) or []
            if not isinstance(items, list):
                raise ValueError(f"supplement {key} is not a JSON array")
            result.dropped += max(0, len(items) - limit)
            for item in items[:limit]:
                try:
                    evidence = item["evidence"]
                    # Grounding always replaces paper and page from the located chunk,
                    # so the model does not have to restate them here.
                    item = {**item, "evidence": {
                        "paper_id": paper.id, "page_start": 1, "page_end": 1,
                        "chunk_id": evidence["chunk_id"], "quote": evidence["quote"],
                    }}
                    target.append(model.model_validate(item))
                except (ValidationError, KeyError, TypeError):
                    result.dropped += 1
        return result

    def _parse(self, raw: str) -> KnowledgeExtraction:
        return KnowledgeExtraction.model_validate_json(_json_text(raw))

    def _ground(
        self,
        payload: KnowledgeExtraction,
        chunks: list[DocumentChunk],
        unlocated: set[tuple[str, str]] | None = None,
    ) -> KnowledgeExtraction:
        by_id = {chunk.id: chunk for chunk in chunks}

        def ground(evidence: EvidenceSpan) -> EvidenceSpan:
            stated = by_id.get(evidence.chunk_id)
            chunk, quote = _locate_in_chunks(
                evidence.quote.strip(),
                [stated, *(item for item in chunks if item is not stated)] if stated else chunks,
            )
            if chunk is None:
                # Never present an unlocated model quote as source text: keep a real
                # excerpt of the cited chunk and flag it for the reviewer.
                chunk = stated or chunks[0]
                quote = _evidence_quote(chunk.text)
                if unlocated is not None:
                    unlocated.add((chunk.id, quote))
            page = int(chunk.metadata.get("page_start", 1) or 1)
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
    """Select coverage across a long paper without inflating the LLM prompt.

    The former first-five/last-two strategy gave no representation to methods,
    experiments, or limitations in the middle of a long PDF.  Keep the bounded
    seven-chunk budget, but reserve the opening and conclusion while spacing the
    remaining selections through the body.
    """

    if len(chunks) <= limit:
        return chunks
    if limit <= 0:
        return []

    opening_count = min(2, limit)
    closing_count = min(2, max(0, limit - opening_count))
    middle_count = limit - opening_count - closing_count
    selected_indexes = list(range(opening_count))
    if middle_count:
        body_size = len(chunks) - opening_count - closing_count
        selected_indexes.extend(
            opening_count
            + ((position + 1) * body_size) // (middle_count + 1)
            - 1
            for position in range(middle_count)
        )
    selected_indexes.extend(range(len(chunks) - closing_count, len(chunks)))
    return [chunks[index] for index in dict.fromkeys(selected_indexes)]


def _json_text(raw: str) -> str:
    cleaned = raw.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?", "", cleaned).strip()
        cleaned = re.sub(r"```$", "", cleaned).strip()
    match = re.search(r"\{.*\}", cleaned, flags=re.DOTALL)
    return match.group(0) if match else cleaned


def _batches(chunks: list[DocumentChunk], limit: int) -> list[list[DocumentChunk]]:
    """Contiguous batches of about ``_BATCH_CHARS``; never more than ``limit`` batches.

    A paper too long for the call budget gets larger batches rather than unread chunks.
    """

    if not chunks or limit <= 0:
        return []
    batches: list[list[DocumentChunk]] = [[]]
    size = 0
    for chunk in chunks:
        if batches[-1] and size + len(chunk.text) > _BATCH_CHARS:
            batches.append([])
            size = 0
        batches[-1].append(chunk)
        size += len(chunk.text)
    if len(batches) <= limit:
        return batches
    bounds = [(index * len(chunks)) // limit for index in range(limit + 1)]
    return [chunks[start:end] for start, end in zip(bounds, bounds[1:], strict=True) if start < end]


def _name_key(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip()).casefold()


def _edge_key(relation: ExtractedRelation) -> tuple[str, str, str]:
    return (_name_key(relation.source_name), _name_key(relation.target_name), relation.type)


def _meaningful_name(value: str) -> bool:
    normalized = re.sub(r"\s+", " ", value.strip()).casefold()
    return len(normalized) >= 3 and normalized not in NOISE_NAMES and not normalized.isdigit()


def _locate_in_chunks(
    quote: str, chunks: list[DocumentChunk]
) -> tuple[DocumentChunk | None, str]:
    """Find the quote modulo layout noise; return the exact source span, not model text."""
    if len(quote) < 16:
        return None, ""
    for chunk in chunks:
        span = locate_quote(quote, chunk.text)
        if span is not None:
            return chunk, re.sub(r"\s+", " ", chunk.text[span[0]:span[1]]).strip()
    return None, ""


def _evidence_quote(text: str) -> str:
    normalized = re.sub(r"\s+", " ", text).strip()
    return normalized[:280] or "正文未提取到可展示的证据。"
