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
# The palette is intentionally shared with the Graph-view preset below.  The values are
# explicit hex colours rather than Obsidian's numbered defaults, so the reading map keeps
# its semantic meaning when a user changes themes.
CANVAS_COLORS = {
    "Paper": "#FF6B6B",
    "Concept": "#36C98E",
    "Method": "#F5A94B",
    "Task": "#7C9CF5",
    "Dataset": "#52C7D9",
    "Metric": "#F4D35E",
    "Finding": "#BB86FC",
}
CANVAS_RELATION_COLORS = {
    "PRESENTS": "#F5A94B",
    "USES": "#52C7D9",
    "ADDRESSES": "#7C9CF5",
    "EVALUATES": "#F4D35E",
    "SUPPORTS": "#36C98E",
    "IMPROVES": "#BB86FC",
    "COMPARES_WITH": "#94A3B8",
    "APPLIES_TO": "#7C9CF5",
    "HAS_LIMITATION": "#FF6B6B",
}
CANVAS_RELATION_PRIORITY = {
    "PRESENTS": 0,
    "USES": 1,
    "ADDRESSES": 2,
    "APPLIES_TO": 3,
    "EVALUATES": 4,
    "SUPPORTS": 5,
    "IMPROVES": 6,
    "COMPARES_WITH": 7,
    "HAS_LIMITATION": 8,
}
CANVAS_TYPE_ORDER = (
    "Paper",
    "Method",
    "Concept",
    "Task",
    "Dataset",
    "Metric",
    "Finding",
)
CANVAS_TYPE_LIMITS = {
    "Paper": 5,
    "Method": 5,
    "Concept": 3,
    "Task": 2,
    "Dataset": 1,
    "Metric": 1,
    "Finding": 1,
}
CANVAS_LANES = (
    ("papers", "论文来源", ("Paper",), "#FF6B6B"),
    ("methods", "方法与技术", ("Method",), "#F5A94B"),
    (
        "knowledge",
        "概念、任务与证据",
        ("Concept", "Task", "Dataset", "Metric", "Finding"),
        "#7C9CF5",
    ),
)
MAX_CANVAS_NODES = 18
MAX_CANVAS_EDGES = 18
MAX_CANVAS_EDGES_PER_NODE = 3

# Graph settings are only written for a new vault. Existing vault settings are personal
# preferences and are never overwritten during a projection.
DEFAULT_OBSIDIAN_GRAPH_CONFIG = {
    "collapse-filter": True,
    "search": '-path:"80_Canvases"',
    "showTags": False,
    "showAttachments": False,
    "hideUnresolved": True,
    "showOrphans": False,
    "collapse-color-groups": False,
    "colorGroups": [
        {"query": 'path:"00_Home"', "color": {"a": 1, "rgb": 9741240}},
        {"query": 'path:"01_Topics"', "color": {"a": 1, "rgb": 8166645}},
        {"query": 'path:"10_Papers"', "color": {"a": 1, "rgb": 16739179}},
        {"query": 'path:"20_Concepts"', "color": {"a": 1, "rgb": 3590542}},
        {"query": 'path:"30_Methods"', "color": {"a": 1, "rgb": 16099659}},
        {"query": 'path:"40_Tasks"', "color": {"a": 1, "rgb": 8166645}},
        {"query": 'path:"50_Datasets"', "color": {"a": 1, "rgb": 5425113}},
        {"query": 'path:"60_Metrics"', "color": {"a": 1, "rgb": 16044894}},
        {"query": 'path:"70_Findings"', "color": {"a": 1, "rgb": 12289788}},
    ],
    "collapse-display": True,
    "showArrow": False,
    "textFadeMultiplier": 1,
    "nodeSizeMultiplier": 0.9,
    "lineSizeMultiplier": 0.7,
    "collapse-forces": True,
    "centerStrength": 0.45,
    "repelStrength": 12,
    "linkStrength": 1,
    "linkDistance": 150,
    "scale": 0.75,
    "close": True,
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
        self._ensure_obsidian_graph_config()
        return folders

    def _ensure_obsidian_graph_config(self) -> None:
        graph_path = self.root / ".obsidian" / "graph.json"
        if graph_path.exists():
            return
        graph_path.parent.mkdir(parents=True, exist_ok=True)
        graph_path.write_text(
            json.dumps(DEFAULT_OBSIDIAN_GRAPH_CONFIG, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

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

        selected = self._canvas_entities(entities, relations, degree)
        selected_ids = {entity.id for entity in selected}

        nodes = []
        positions: dict[str, tuple[int, int, int, int]] = {}
        active_lanes = [
            lane
            for lane in CANVAS_LANES
            if any(entity.type in lane[2] for entity in selected)
        ]
        canvas_width = max(380, len(active_lanes) * 460 - 60)
        nodes.append(
            {
                "id": "canvas-guide",
                "type": "text",
                "x": 0,
                "y": -150,
                "width": canvas_width,
                "height": 100,
                "text": (
                    f"# {topic}\n"
                    "论文 → 方法 → 知识要素 · 点击卡片打开笔记\n"
                    "连线颜色：提出（橙）· 使用（青）· 解决（蓝）· 评估（黄）· 支持（绿）"
                ),
                "color": "#94A3B8",
            }
        )
        for lane_index, (_, label, entity_types, group_color) in enumerate(active_lanes):
            group = sorted(
                [entity for entity in selected if entity.type in entity_types],
                key=lambda item: (-degree.get(item.id, 0), item.name.casefold()),
            )
            x = lane_index * 460
            nodes.append(
                {
                    "id": f"group-{lane_index}",
                    "type": "group",
                    "x": x - 25,
                    "y": 0,
                    "width": 430,
                    "height": max(210, len(group) * 148 + 80),
                    "label": label,
                    "color": group_color,
                }
            )
            for index, entity in enumerate(group):
                path = note_paths[entity.id].relative_to(self.root).as_posix()
                y = 50 + index * 148
                nodes.append(
                    {
                        "id": entity.id,
                        "type": "file",
                        "file": path,
                        "x": x,
                        "y": y,
                        "width": 380,
                        "height": 120,
                        "color": CANVAS_COLORS.get(entity.type, "#94A3B8"),
                    }
                )
                positions[entity.id] = (x, y, 380, 120)

        edges = self._canvas_edges(relations, selected_ids, positions)
        return {
            "nodes": nodes,
            "edges": edges,
            "metadata": {"topic": topic, "version": 2, "layout": "reading-flow"},
        }

    def _canvas_entities(
        self,
        entities: list[PublishedEntity],
        relations: list[PublishedRelation],
        degree: dict[str, int],
    ) -> list[PublishedEntity]:
        selected: list[PublishedEntity] = []
        selected_ids: set[str] = set()
        by_id = {entity.id: entity for entity in entities}
        type_counts: dict[str, int] = {}

        def ranked(values: list[PublishedEntity]) -> list[PublishedEntity]:
            return sorted(
                values, key=lambda item: (-degree.get(item.id, 0), item.name.casefold())
            )

        def can_add(entity: PublishedEntity) -> bool:
            return (
                entity.id in selected_ids
                or type_counts.get(entity.type, 0) < CANVAS_TYPE_LIMITS.get(entity.type, 1)
            )

        def add(entity: PublishedEntity) -> None:
            if entity.id not in selected_ids:
                selected.append(entity)
                selected_ids.add(entity.id)
                type_counts[entity.type] = type_counts.get(entity.type, 0) + 1

        # Choose connected pairs first. Ranking isolated high-degree nodes independently
        # can otherwise yield a visually empty Canvas for a densely connected collection.
        relation_candidates = sorted(
            relations,
            key=lambda item: (
                -item.confidence,
                CANVAS_RELATION_PRIORITY.get(item.type, 99),
                item.id,
            ),
        )
        for relation in relation_candidates:
            source = by_id.get(relation.source_entity_id)
            target = by_id.get(relation.target_entity_id)
            if source is None or target is None:
                continue
            newly_added = {source.id, target.id} - selected_ids
            if len(selected) + len(newly_added) > MAX_CANVAS_NODES:
                continue
            new_type_counts: dict[str, int] = {}
            for entity_id in newly_added:
                entity = by_id[entity_id]
                new_type_counts[entity.type] = new_type_counts.get(entity.type, 0) + 1
            if any(
                type_counts.get(entity_type, 0) + count
                > CANVAS_TYPE_LIMITS.get(entity_type, 1)
                for entity_type, count in new_type_counts.items()
            ):
                continue
            add(source)
            add(target)

        for entity_type in CANVAS_TYPE_ORDER:
            candidates = ranked([entity for entity in entities if entity.type == entity_type])
            for entity in candidates:
                if not can_add(entity):
                    continue
                add(entity)
        return selected

    def _canvas_edges(
        self,
        relations: list[PublishedRelation],
        selected_ids: set[str],
        positions: dict[str, tuple[int, int, int, int]],
    ) -> list[dict]:
        candidates = [
            relation
            for relation in relations
            if relation.confidence >= 0.65
            and relation.source_entity_id in selected_ids
            and relation.target_entity_id in selected_ids
        ]
        candidates.sort(
            key=lambda item: (
                -item.confidence,
                CANVAS_RELATION_PRIORITY.get(item.type, 99),
                item.id,
            )
        )

        edges: list[dict] = []
        edge_degree: dict[str, int] = {}
        for relation in candidates:
            source_id = relation.source_entity_id
            target_id = relation.target_entity_id
            if (
                edge_degree.get(source_id, 0) >= MAX_CANVAS_EDGES_PER_NODE
                or edge_degree.get(target_id, 0) >= MAX_CANVAS_EDGES_PER_NODE
            ):
                continue
            from_side, to_side = _edge_sides(positions[source_id], positions[target_id])
            edges.append(
                {
                    "id": relation.id,
                    "fromNode": source_id,
                    "fromSide": from_side,
                    "toNode": target_id,
                    "toSide": to_side,
                    "toEnd": "arrow",
                    "color": CANVAS_RELATION_COLORS.get(relation.type, "#94A3B8"),
                }
            )
            edge_degree[source_id] = edge_degree.get(source_id, 0) + 1
            edge_degree[target_id] = edge_degree.get(target_id, 0) + 1
            if len(edges) >= MAX_CANVAS_EDGES:
                break
        return edges

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


def _edge_sides(
    source: tuple[int, int, int, int], target: tuple[int, int, int, int]
) -> tuple[str, str]:
    source_x, source_y, source_width, source_height = source
    target_x, target_y, target_width, target_height = target
    source_center = (source_x + source_width / 2, source_y + source_height / 2)
    target_center = (target_x + target_width / 2, target_y + target_height / 2)
    horizontal_distance = target_center[0] - source_center[0]
    vertical_distance = target_center[1] - source_center[1]
    if abs(horizontal_distance) >= abs(vertical_distance):
        return ("right", "left") if horizontal_distance >= 0 else ("left", "right")
    return ("bottom", "top") if vertical_distance >= 0 else ("top", "bottom")
