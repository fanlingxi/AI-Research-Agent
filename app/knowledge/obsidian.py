from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from app.knowledge.schemas import PublishedEntity, PublishedRelation

FOLDER_BY_TYPE = {
    "Paper": "10_Papers",
    "Concept": "20_Concepts",
    "Method": "30_Methods",
    "Task": "40_Tasks",
    "Dataset": "50_Datasets",
    "Metric": "60_Metrics",
    "Finding": "70_Findings",
    "Topic": "01_Topics",
}
TYPE_LABELS = {
    "Paper": "论文",
    "Topic": "主题",
    "Concept": "概念",
    "Method": "方法",
    "Task": "任务",
    "Dataset": "数据集",
    "Metric": "指标",
    "Finding": "研究发现",
}
RELATION_LABELS = {
    "PRESENTS": "提出",
    "ADDRESSES": "解决",
    "USES": "使用",
    "EVALUATES": "评估",
    "IMPROVES": "改进",
    "COMPARES_WITH": "对比",
    "APPLIES_TO": "应用于",
    "HAS_LIMITATION": "存在局限",
    "SUPPORTS": "支持",
}
CANVAS_COLORS = {
    "Paper": "1",
    "Concept": "4",
    "Method": "2",
    "Task": "6",
    "Dataset": "5",
    "Metric": "3",
    "Finding": "6",
}


@dataclass(frozen=True)
class KnowledgeVaultResult:
    vault_path: str
    home_path: str
    topic_path: str
    canvas_path: str


class KnowledgeVaultExporter:
    """Render published knowledge as readable notes; drafts never enter this vault."""

    def __init__(self, vault_path: str) -> None:
        self.root = Path(vault_path)

    def render_topic(
        self,
        *,
        topic: str,
        topic_slug: str,
        entities: list[PublishedEntity],
        relations: list[PublishedRelation],
    ) -> KnowledgeVaultResult:
        folders = self._ensure_folders()
        note_paths = self._note_paths(entities, folders)
        by_id = {entity.id: entity for entity in entities}
        related_by_entity: dict[str, list[PublishedRelation]] = {
            entity.id: [] for entity in entities
        }
        for relation in relations:
            if relation.source_entity_id in related_by_entity:
                related_by_entity[relation.source_entity_id].append(relation)
            if relation.target_entity_id in related_by_entity:
                related_by_entity[relation.target_entity_id].append(relation)

        for entity in entities:
            content = self._entity_note(
                entity=entity,
                relations=related_by_entity.get(entity.id, []),
                entities=by_id,
                note_paths=note_paths,
            )
            self._write_managed_note(note_paths[entity.id], content)

        topic_path = folders["topics"] / f"{_file_name(topic)}.md"
        self._write_managed_note(
            topic_path,
            self._topic_note(topic, entities, relations, note_paths),
        )
        canvas_path = folders["canvases"] / f"{_file_name(topic)}.canvas"
        canvas_path.write_text(
            json.dumps(
                self._canvas(topic, entities, relations, note_paths),
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        home_path = folders["home"] / "知识库首页.md"
        self._write_managed_note(home_path, self._home_note())
        return KnowledgeVaultResult(
            vault_path=str(self.root),
            home_path=str(home_path),
            topic_path=str(topic_path),
            canvas_path=str(canvas_path),
        )

    def _ensure_folders(self) -> dict[str, Path]:
        folders = {
            "home": self.root / "00_Home",
            "topics": self.root / "01_Topics",
            "papers": self.root / "10_Papers",
            "concepts": self.root / "20_Concepts",
            "methods": self.root / "30_Methods",
            "tasks": self.root / "40_Tasks",
            "datasets": self.root / "50_Datasets",
            "metrics": self.root / "60_Metrics",
            "findings": self.root / "70_Findings",
            "canvases": self.root / "80_Canvases",
        }
        for folder in folders.values():
            folder.mkdir(parents=True, exist_ok=True)
        return folders

    def _note_paths(
        self, entities: list[PublishedEntity], folders: dict[str, Path]
    ) -> dict[str, Path]:
        used: set[Path] = set()
        paths: dict[str, Path] = {}
        for entity in sorted(entities, key=lambda item: (item.type, item.name.casefold())):
            folder_key = FOLDER_BY_TYPE[entity.type].split("_")[-1].lower()
            folder = folders[folder_key]
            base = _file_name(entity.name)
            path = folder / f"{base}.md"
            suffix = 2
            while path in used:
                path = folder / f"{base} ({suffix}).md"
                suffix += 1
            used.add(path)
            paths[entity.id] = path
        return paths

    def _entity_note(
        self,
        *,
        entity: PublishedEntity,
        relations: list[PublishedRelation],
        entities: dict[str, PublishedEntity],
        note_paths: dict[str, Path],
    ) -> str:
        frontmatter = _frontmatter(
            {
                "id": entity.id,
                "type": entity.type,
                "aliases": entity.aliases,
                "status": "published",
                "topic_slugs": entity.topic_slugs,
                "evidence_count": len(entity.evidence),
            }
        )
        if entity.type == "Paper":
            body = self._paper_body(entity, relations, entities, note_paths)
        else:
            body = self._knowledge_body(entity, relations, entities, note_paths)
        return frontmatter + body

    def _paper_body(
        self,
        entity: PublishedEntity,
        relations: list[PublishedRelation],
        entities: dict[str, PublishedEntity],
        note_paths: dict[str, Path],
    ) -> str:
        reading = entity.metadata.get("reading", {})
        contributions = _bullets(reading.get("core_contributions", []))
        results = _bullets(reading.get("key_results", []))
        limitations = _bullets(reading.get("limitations", []))
        return "\n".join(
            [
                f"# {entity.name}",
                "",
                "## 研究问题",
                "",
                reading.get("research_problem", entity.summary),
                "",
                "## 核心贡献",
                "",
                contributions,
                "",
                "## 方法",
                "",
                reading.get("method_summary", entity.summary),
                "",
                "## 关键结果",
                "",
                results,
                "",
                "## 局限性",
                "",
                limitations,
                "",
                "## 关联知识",
                "",
                self._relation_links(entity.id, relations, entities, note_paths),
                "",
                "## 证据来源",
                "",
                self._evidence_lines(entity.evidence),
                "",
            ]
        )

    def _knowledge_body(
        self,
        entity: PublishedEntity,
        relations: list[PublishedRelation],
        entities: dict[str, PublishedEntity],
        note_paths: dict[str, Path],
    ) -> str:
        return "\n".join(
            [
                f"# {entity.name}",
                "",
                "## 一句话定义",
                "",
                entity.summary,
                "",
                "## 别名",
                "",
                _bullets(entity.aliases),
                "",
                "## 关联知识",
                "",
                self._relation_links(entity.id, relations, entities, note_paths),
                "",
                "## 证据",
                "",
                self._evidence_lines(entity.evidence),
                "",
            ]
        )

    def _relation_links(
        self,
        entity_id: str,
        relations: list[PublishedRelation],
        entities: dict[str, PublishedEntity],
        note_paths: dict[str, Path],
    ) -> str:
        lines = []
        for relation in sorted(relations, key=lambda item: (-item.confidence, item.type)):
            other_id = (
                relation.target_entity_id
                if relation.source_entity_id == entity_id
                else relation.source_entity_id
            )
            other = entities.get(other_id)
            if other is None or other_id not in note_paths:
                continue
            link = self._note_link(note_paths[other_id], other.name)
            lines.append(f"- **{RELATION_LABELS[relation.type]}** {link}：{relation.summary}")
        return "\n".join(lines) or "- 暂无已发布的关联知识。"

    def _evidence_lines(self, evidence) -> str:
        lines = []
        for item in evidence:
            pages = (
                f"p. {item.page_start}"
                if item.page_start == item.page_end
                else f"pp. {item.page_start}-{item.page_end}"
            )
            lines.extend([f"- `{pages}` · `{item.chunk_id}`", f"  > {item.quote}"])
        return "\n".join(lines) or "- 暂无可展示的证据。"

    def _topic_note(
        self,
        topic: str,
        entities: list[PublishedEntity],
        relations: list[PublishedRelation],
        note_paths: dict[str, Path],
    ) -> str:
        papers = [entity for entity in entities if entity.type == "Paper"]
        concepts = [entity for entity in entities if entity.type != "Paper"]
        canvas_path = Path("80_Canvases") / f"{_file_name(topic)}.canvas"
        return _frontmatter(
            {"type": "topic_moc", "status": "published", "title": topic}
        ) + "\n".join(
            [
                f"# {topic}",
                "",
                "## 主题概览",
                "",
                (
                    f"本主题已收录 {len(papers)} 篇论文、{len(concepts)} 个已审核知识节点和 "
                    f"{len(relations)} 条语义关系。"
                ),
                "",
                "## 可视化地图",
                "",
                f"![[{canvas_path.as_posix()}]]",
                "",
                "## 核心论文",
                "",
                _bullets(self._note_link(note_paths[item.id], item.name) for item in papers),
                "",
                "## 核心知识",
                "",
                _bullets(self._note_link(note_paths[item.id], item.name) for item in concepts),
                "",
                "## 使用方式",
                "",
                "从一篇论文或一个概念打开 Local Graph；Canvas 只展示审核后的高价值关系。",
                "",
            ]
        )

    def _home_note(self) -> str:
        return _frontmatter({"type": "knowledge_home", "status": "published"}) + "\n".join(
            [
                "# 本地研究知识库",
                "",
                "这里保存经过人工审核的研究知识。候选内容在 Streamlit 审核队列中处理，"
                "不会直接进入此 Vault。",
                "",
                "## 导航",
                "",
                "- [[01_Topics|按主题阅读]]",
                "- [[10_Papers|按论文阅读]]",
                "- [[80_Canvases|查看主题 Canvas]]",
                "",
            ]
        )

    def _note_link(self, path: Path, display: str) -> str:
        return _wiki_link(path.relative_to(self.root), display)

    def _canvas(
        self,
        topic: str,
        entities: list[PublishedEntity],
        relations: list[PublishedRelation],
        note_paths: dict[str, Path],
    ) -> dict:
        degree: dict[str, int] = {entity.id: 0 for entity in entities}
        for relation in relations:
            degree[relation.source_entity_id] = degree.get(relation.source_entity_id, 0) + 1
            degree[relation.target_entity_id] = degree.get(relation.target_entity_id, 0) + 1
        selected = sorted(
            entities,
            key=lambda item: (item.type != "Paper", -degree.get(item.id, 0), item.name.casefold()),
        )[:25]
        selected_ids = {entity.id for entity in selected}
        grouped: dict[str, list[PublishedEntity]] = {}
        for entity in selected:
            grouped.setdefault(entity.type, []).append(entity)

        nodes = []
        for group_index, (entity_type, group) in enumerate(sorted(grouped.items())):
            x = group_index * 440
            nodes.append(
                {
                    "id": f"group-{entity_type}",
                    "type": "group",
                    "x": x - 25,
                    "y": -60,
                    "width": 390,
                    "height": max(250, len(group) * 220 + 100),
                    "label": TYPE_LABELS[entity_type],
                    "color": CANVAS_COLORS.get(entity_type, "4"),
                }
            )
            for index, entity in enumerate(group):
                path = note_paths[entity.id].relative_to(self.root).as_posix()
                nodes.append(
                    {
                        "id": entity.id,
                        "type": "file",
                        "file": path,
                        "x": x,
                        "y": index * 220,
                        "width": 340,
                        "height": 160,
                        "color": CANVAS_COLORS.get(entity.type, "4"),
                    }
                )
        edges = [
            {
                "id": relation.id,
                "fromNode": relation.source_entity_id,
                "toNode": relation.target_entity_id,
                "toEnd": "arrow",
                "label": RELATION_LABELS[relation.type],
                "color": "2" if relation.confidence >= 0.8 else "4",
            }
            for relation in sorted(relations, key=lambda item: -item.confidence)
            if relation.confidence >= 0.65
            and relation.source_entity_id in selected_ids
            and relation.target_entity_id in selected_ids
        ][:35]
        return {"nodes": nodes, "edges": edges, "metadata": {"topic": topic, "version": 1}}

    def _write_managed_note(self, path: Path, generated: str) -> None:
        manual = ""
        if path.exists():
            existing = path.read_text(encoding="utf-8")
            marker = "## 人工笔记"
            if marker in existing:
                manual = existing.split(marker, maxsplit=1)[1].strip()
        content = (
            generated.rstrip()
            + "\n\n## 人工笔记\n\n"
            + (manual or "在这里记录你的阅读、判断和待验证问题。")
        )
        path.write_text(content.rstrip() + "\n", encoding="utf-8")


def _frontmatter(values: dict[str, object]) -> str:
    lines = ["---"]
    for key, value in values.items():
        if value in (None, "", []):
            continue
        if isinstance(value, list):
            lines.append(f"{key}:")
            lines.extend(f"  - {json.dumps(str(item), ensure_ascii=False)}" for item in value)
        else:
            lines.append(f"{key}: {json.dumps(str(value), ensure_ascii=False)}")
    lines.extend(["updated_at: " + datetime.now(tz=UTC).isoformat(), "---", ""])
    return "\n".join(lines)


def _bullets(items) -> str:
    values = [str(item).strip() for item in items if str(item).strip()]
    return "\n".join(f"- {item}" for item in values) if values else "- 暂无。"


def _file_name(value: str) -> str:
    cleaned = re.sub(r"[\\/:*?\"<>|\[\]#^]", "-", value).strip().rstrip(".")
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned[:100] or "未命名知识"


def _wiki_link(path: Path, display: str) -> str:
    without_suffix = path.with_suffix("").as_posix()
    return f"[[{without_suffix}|{display}]]"
