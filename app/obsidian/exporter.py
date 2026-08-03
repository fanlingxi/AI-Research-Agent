from __future__ import annotations

import hashlib
import json
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from app.schemas.documents import PaperMetadata, RetrievalHit
from app.schemas.graph import GraphEntity, GraphRelation
from app.schemas.quality import EvaluationResult


@dataclass(frozen=True)
class ObsidianExportResult:
    """Paths emitted for one reviewable, source-governed research run."""

    exported: bool
    reason: str
    run_id: str
    vault_path: str
    run_note_path: str | None = None
    report_note_path: str | None = None
    graph_json_path: str | None = None
    graphml_path: str | None = None

    def model_dump(self) -> dict[str, str | bool | None]:
        return {
            "exported": self.exported,
            "reason": self.reason,
            "run_id": self.run_id,
            "vault_path": self.vault_path,
            "run_note_path": self.run_note_path,
            "report_note_path": self.report_note_path,
            "graph_json_path": self.graph_json_path,
            "graphml_path": self.graphml_path,
        }


class ObsidianVaultExporter:
    """Write a one-way, human-reviewable view of formal research evidence."""

    def __init__(self, vault_path: str, review_status: str = "pending") -> None:
        self.root = Path(vault_path)
        self.review_status = review_status

    def export(
        self,
        *,
        run_id: str,
        query: str,
        report: str,
        evaluation: EvaluationResult,
        papers: list[PaperMetadata],
        retrieval_hits: list[RetrievalHit],
        entities: list[GraphEntity],
        relations: list[GraphRelation],
    ) -> ObsidianExportResult:
        if not evaluation.evidence_admissible:
            return ObsidianExportResult(
                exported=False,
                reason="证据未达到正式沉淀门槛，跳过 Obsidian 导出。",
                run_id=run_id,
                vault_path=str(self.root),
            )

        folders = self._ensure_folders()
        selected_entities, selected_relations = _curate_graph(
            entities=entities,
            relations=relations,
            retrieval_hits=retrieval_hits,
        )
        paper_stems = {paper.id: _note_stem("paper", paper.id) for paper in papers}
        entity_stems = {
            entity.id: _note_stem(entity.type.lower(), entity.id)
            for entity in selected_entities
        }

        for paper in papers:
            stem = paper_stems[paper.id]
            self._write_note(
                folders["papers"] / f"{stem}.md",
                _frontmatter(
                    {
                        "id": paper.id,
                        "type": "paper",
                        "title": paper.title,
                        "source_tier": paper.source_tier,
                        "source": paper.source,
                        "source_url": paper.url,
                        "pdf_url": paper.pdf_url,
                        "local_path": paper.metadata.get("local_path"),
                        "doi": paper.doi,
                        "publication": paper.publication,
                        "year": paper.year,
                        "review_status": self.review_status,
                    }
                )
                + f"# {paper.title}\n\n"
                + "## 摘要\n\n"
                + (paper.abstract or "未提供摘要。")
                + "\n\n## 来源\n\n"
                + _source_links(paper),
            )

        for entity in selected_entities:
            stem = entity_stems[entity.id]
            relation_links = [
                _wiki_link(entity_stems[relation.target_id])
                if relation.source_id == entity.id and relation.target_id in entity_stems
                else _wiki_link(entity_stems[relation.source_id])
                for relation in selected_relations
                if entity.id in {relation.source_id, relation.target_id}
            ]
            source_links = _links_for_source_chunks(
                entity.source_chunk_ids,
                retrieval_hits,
                paper_stems,
            )
            self._write_note(
                folders["concepts"] / f"{stem}.md",
                _frontmatter(
                    {
                        "id": entity.id,
                        "type": entity.type,
                        "title": entity.name,
                        "evidence_ids": entity.source_chunk_ids,
                        "confidence": _entity_confidence(entity),
                        "review_status": self.review_status,
                    }
                )
                + f"# {entity.name}\n\n"
                + "## 描述\n\n"
                + (entity.description or "自动抽取的研究实体。")
                + "\n\n## 关联知识\n\n"
                + _bullet_links(relation_links)
                + "\n\n## 证据\n\n"
                + _bullet_links(source_links),
            )

        for relation in selected_relations:
            stem = _note_stem("claim", relation.id)
            source_link = _wiki_link(entity_stems[relation.source_id])
            target_link = _wiki_link(entity_stems[relation.target_id])
            evidence_links = _links_for_source_chunks(
                relation.source_chunk_ids,
                retrieval_hits,
                paper_stems,
            )
            self._write_note(
                folders["claims"] / f"{stem}.md",
                _frontmatter(
                    {
                        "id": relation.id,
                        "type": "claim",
                        "relation": relation.type,
                        "source_entity": relation.source_id,
                        "target_entity": relation.target_id,
                        "weight": relation.weight,
                        "evidence_ids": relation.source_chunk_ids,
                        "review_status": self.review_status,
                    }
                )
                + f"# {relation.type}\n\n"
                + f"{source_link} → **{relation.type}** → {target_link}\n\n"
                + "## 说明\n\n"
                + (relation.description or "自动抽取的关联声明。")
                + "\n\n## 证据\n\n"
                + _bullet_links(evidence_links),
            )

        report_stem = _note_stem("report", run_id)
        report_path = folders["reports"] / f"{report_stem}.md"
        self._write_note(
            report_path,
            _frontmatter(
                {
                    "id": run_id,
                    "type": "research_report",
                    "query": query,
                    "quality_score": evaluation.overall_score,
                    "evidence_status": evaluation.evidence_status,
                    "evidence_admissible": evaluation.evidence_admissible,
                    "review_status": self.review_status,
                    "evidence_ids": [hit.chunk_id for hit in retrieval_hits],
                }
            )
            + report.rstrip()
            + "\n\n## 关联论文\n\n"
            + _bullet_links(_wiki_link(paper_stems[paper.id]) for paper in papers),
        )

        run_stem = _note_stem("run", run_id)
        run_path = folders["runs"] / f"{run_stem}.md"
        self._write_note(
            run_path,
            _frontmatter(
                {
                    "id": run_id,
                    "type": "research_run",
                    "query": query,
                    "quality_score": evaluation.overall_score,
                    "evidence_status": evaluation.evidence_status,
                    "review_status": self.review_status,
                }
            )
            + f"# 研究运行：{query}\n\n"
            + "## 报告\n\n"
            + _wiki_link(report_stem)
            + "\n\n## 论文\n\n"
            + _bullet_links(_wiki_link(paper_stems[paper.id]) for paper in papers)
            + "\n\n## 关键概念\n\n"
            + _bullet_links(_wiki_link(entity_stems[entity.id]) for entity in selected_entities),
        )

        graph_dir = folders["exports"] / _safe_name(run_id)
        graph_dir.mkdir(parents=True, exist_ok=True)
        graph_json = graph_dir / "graph.json"
        graphml = graph_dir / "graph.graphml"
        _write_graph_json(graph_json, run_id, query, selected_entities, selected_relations)
        _write_graphml(graphml, selected_entities, selected_relations)
        return ObsidianExportResult(
            exported=True,
            reason="已导出可审阅的 Obsidian 知识笔记和图谱工件。",
            run_id=run_id,
            vault_path=str(self.root),
            run_note_path=str(run_path),
            report_note_path=str(report_path),
            graph_json_path=str(graph_json),
            graphml_path=str(graphml),
        )

    def _ensure_folders(self) -> dict[str, Path]:
        folders = {
            "runs": self.root / "00_Runs",
            "papers": self.root / "10_Papers",
            "concepts": self.root / "20_Concepts",
            "claims": self.root / "30_Claims",
            "reports": self.root / "40_Reports",
            "exports": self.root / "Exports",
        }
        for folder in folders.values():
            folder.mkdir(parents=True, exist_ok=True)
        return folders

    def _write_note(self, path: Path, content: str) -> None:
        path.write_text(content.rstrip() + "\n", encoding="utf-8")


def build_run_id(query: str) -> str:
    digest = hashlib.sha1(query.encode("utf-8")).hexdigest()[:10]
    timestamp = datetime.now(tz=UTC).strftime("%Y%m%dT%H%M%SZ")
    return f"run-{timestamp}-{digest}"


def _curate_graph(
    entities: list[GraphEntity],
    relations: list[GraphRelation],
    retrieval_hits: list[RetrievalHit],
) -> tuple[list[GraphEntity], list[GraphRelation]]:
    evidence_ids = {hit.chunk_id for hit in retrieval_hits}
    selected_relations = [
        relation for relation in relations if evidence_ids.intersection(relation.source_chunk_ids)
    ][:80]
    selected_ids = {
        entity_id
        for relation in selected_relations
        for entity_id in (relation.source_id, relation.target_id)
    }
    selected_entities = [entity for entity in entities if entity.id in selected_ids][:60]
    selected_ids = {entity.id for entity in selected_entities}
    selected_relations = [
        relation
        for relation in selected_relations
        if relation.source_id in selected_ids and relation.target_id in selected_ids
    ]
    return selected_entities, selected_relations


def _frontmatter(values: dict[str, object]) -> str:
    lines = ["---"]
    for key, value in values.items():
        if value is None or value == "":
            continue
        if isinstance(value, list):
            lines.append(f"{key}:")
            lines.extend(f"  - {_yaml_value(item)}" for item in value)
        else:
            lines.append(f"{key}: {_yaml_value(value)}")
    lines.extend(["updated_at: " + datetime.now(tz=UTC).isoformat(), "---", ""])
    return "\n".join(lines)


def _yaml_value(value: object) -> str:
    return json.dumps(str(value), ensure_ascii=False)


def _note_stem(prefix: str, identifier: str) -> str:
    return f"{prefix}--{_safe_name(identifier)}"


def _safe_name(value: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9_-]+", "-", value).strip("-").lower()
    return normalized[:100] or "untitled"


def _wiki_link(stem: str) -> str:
    return f"[[{stem}]]"


def _bullet_links(links) -> str:
    items = list(dict.fromkeys(link for link in links if link))
    return "\n".join(f"- {item}" for item in items) if items else "- 暂无可链接条目。"


def _source_links(paper: PaperMetadata) -> str:
    links = []
    if paper.url:
        links.append(f"- 在线记录：{paper.url}")
    if paper.pdf_url:
        links.append(f"- PDF：{paper.pdf_url}")
    local_path = paper.metadata.get("local_path")
    if local_path:
        links.append(f"- 本地文件：`{local_path}`")
    if paper.doi:
        links.append(f"- DOI：{paper.doi}")
    return "\n".join(links) or "- 未提供可访问来源链接。"


def _links_for_source_chunks(
    chunk_ids: list[str],
    hits: list[RetrievalHit],
    paper_stems: dict[str, str],
) -> list[str]:
    source_ids = set(chunk_ids)
    matching = [hit for hit in hits if hit.chunk_id in source_ids]
    return [
        _wiki_link(paper_stems[hit.paper_id])
        for hit in matching
        if hit.paper_id in paper_stems
    ]


def _entity_confidence(entity: GraphEntity) -> float:
    return round(min(1.0, 0.4 + 0.15 * len(entity.source_chunk_ids)), 2)


def _write_graph_json(
    path: Path,
    run_id: str,
    query: str,
    entities: list[GraphEntity],
    relations: list[GraphRelation],
) -> None:
    path.write_text(
        json.dumps(
            {
                "run_id": run_id,
                "query": query,
                "nodes": [entity.model_dump() for entity in entities],
                "edges": [relation.model_dump() for relation in relations],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


def _write_graphml(path: Path, entities: list[GraphEntity], relations: list[GraphRelation]) -> None:
    graphml = ET.Element("graphml", xmlns="http://graphml.graphdrawing.org/xmlns")
    graph = ET.SubElement(graphml, "graph", id="research_graph", edgedefault="directed")
    for entity in entities:
        node = ET.SubElement(graph, "node", id=entity.id)
        ET.SubElement(node, "data", key="label").text = entity.name
        ET.SubElement(node, "data", key="type").text = entity.type
    for relation in relations:
        edge = ET.SubElement(
            graph,
            "edge",
            id=relation.id,
            source=relation.source_id,
            target=relation.target_id,
        )
        ET.SubElement(edge, "data", key="relation").text = relation.type
        ET.SubElement(edge, "data", key="weight").text = str(relation.weight)
    ET.indent(graphml, space="  ")
    ET.ElementTree(graphml).write(path, encoding="utf-8", xml_declaration=True)
