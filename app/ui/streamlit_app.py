from __future__ import annotations

import os
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import streamlit as st

API_BASE_URL = os.getenv("AI_RESEARCH_API_URL", "http://localhost:8000")
REACT_WORKSPACE_URL = os.getenv("AI_RESEARCH_REACT_URL", "http://localhost:5173")
BEIJING_TIMEZONE = ZoneInfo("Asia/Shanghai")
INGESTION_STATUS_LABELS = {
    "queued": "排队中",
    "running": "处理中",
    "needs_review": "等待审核",
    "publishing": "正在发布",
    "completed": "已完成",
    "failed": "处理失败",
    "interrupted": "已中断",
}

st.set_page_config(
    page_title="Research Operations Console",
    page_icon="◈",
    layout="wide",
    initial_sidebar_state="expanded",
)
st.markdown(
    """
    <style>
      .stApp { background: #f4f5f2; }
      [data-testid="stSidebar"] { background: #102f31; }
      [data-testid="stSidebar"] * { color: #edf5f3; }
      [data-testid="stMetric"] {
        background: #ffffff; border: 1px solid #e3e9e6; border-radius: 14px; padding: 14px;
      }
      .ops-hero {
        background: linear-gradient(130deg, #123e40, #1b6769); border-radius: 18px;
        color: #f5fbfa; padding: 28px 30px; margin-bottom: 1.4rem;
      }
      .ops-hero__eyebrow {
        color: #b9ddda; font-size: .78rem; font-weight: 700; letter-spacing: .12em;
        text-transform: uppercase; margin: 0 0 .35rem;
      }
      .ops-hero h1 { color: #ffffff; font-size: 2rem; margin: 0; }
      .ops-hero p { color: #dcefed; margin: .65rem 0 0; max-width: 54rem; }
      .ops-note {
        background: #e8f2ef; border-left: 4px solid #1b6769; border-radius: 8px;
        color: #234447; padding: .75rem 1rem;
      }
      .stButton > button, .stLinkButton > a { border-radius: 9px; font-weight: 600; }
    </style>
    <section class="ops-hero">
      <p class="ops-hero__eyebrow">Research Knowledge Platform</p>
      <h1>Operations Console</h1>
      <p>用于队列观测、任务恢复、审核诊断和证据验收。
      日常入库、检索与报告工作请在 React 主工作台完成。</p>
    </section>
    """,
    unsafe_allow_html=True,
)


def _api(method: str, path: str, **kwargs: Any) -> Any:
    response = httpx.request(method, f"{API_BASE_URL}{path}", timeout=60.0, **kwargs)
    response.raise_for_status()
    return response.json()


def _show_api_error(exc: httpx.HTTPError) -> None:
    detail = ""
    if isinstance(exc, httpx.HTTPStatusError):
        try:
            detail = str(exc.response.json().get("detail", ""))
        except ValueError:
            detail = exc.response.text
    st.error(f"请求失败：{detail or exc}")


def _format_beijing_time(value: str | None) -> str:
    """Keep storage in UTC while consistently presenting the workspace in Beijing time."""

    if not value:
        return "—"
    try:
        timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return value
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=UTC)
    return timestamp.astimezone(BEIJING_TIMEZONE).strftime("%Y-%m-%d %H:%M:%S")


def _ingestion_progress(item: dict[str, Any]) -> tuple[int, str]:
    status = item["status"]
    source_count = len(item.get("sources", []))
    document_count = min(item.get("document_count", 0), source_count)
    candidate_count = item.get("candidate_count", 0)
    if status == "queued":
        position = item.get("queue_position")
        suffix = f"，当前第 {position} 位" if position else ""
        return 5, f"已入队，等待 worker 领取{suffix}。"
    if status == "running":
        if document_count < source_count:
            return (
                max(10, int(10 + 45 * document_count / max(source_count, 1))),
                f"正在解析 PDF：已完成 {document_count} / {source_count} 篇。",
            )
        if candidate_count:
            return (
                70,
                f"已解析 {document_count} / {source_count} 篇，"
                f"正在抽取候选（{candidate_count} 个）。",
            )
        return 60, f"已解析 {document_count} / {source_count} 篇，正在索引并抽取候选。"
    if status == "needs_review":
        return 80, f"解析和候选抽取完成，共 {candidate_count} 个候选，等待人工审核。"
    if status == "publishing":
        return 90, "审核已完成，正在发布正式知识投影。"
    if status == "completed":
        return 100, "任务已完成。"
    if status == "failed":
        return 100, "任务失败；请查看错误并在修复后重试。"
    return 100, "任务被中断；可在任务历史中重新排队。"


def _render_ingestion_status_card(item: dict[str, Any], *, key_prefix: str) -> None:
    status = item["status"]
    progress, message = _ingestion_progress(item)
    source_count = len(item.get("sources", []))
    status_label = INGESTION_STATUS_LABELS.get(status, status)
    if status in {"failed", "interrupted"}:
        st.error(f"{status_label} · {message}")
    elif status == "completed":
        st.success(f"{status_label} · {message}")
    else:
        st.info(f"{status_label} · {message}")
    st.progress(progress, text=message)
    metrics = st.columns(4)
    metrics[0].metric("PDF", f"{item.get('document_count', 0)} / {source_count}")
    metrics[1].metric("候选", item.get("candidate_count", 0))
    metrics[2].metric("已发布", item.get("published_count", 0))
    if status == "queued" and item.get("queue_position"):
        metrics[3].metric("队列位置", f"第 {item['queue_position']} 位")
    else:
        metrics[3].metric("执行尝试", item.get("job_attempts", 0))
    st.caption(
        f"任务 ID：{item['id']} · 提交于 {_format_beijing_time(item.get('created_at'))} 北京时间"
        f" · 更新于 {_format_beijing_time(item.get('updated_at'))} 北京时间"
    )
    if item.get("error"):
        st.warning(f"处理说明：{item['error']}")
    if status in {"queued", "running", "publishing"} and st.button(
        "刷新任务进度", key=f"{key_prefix}_refresh_{item['id']}"
    ):
        st.rerun()


def _render_operations_overview() -> None:
    """Keep Streamlit focused on operational visibility, not daily research work."""

    st.subheader("运营概览")
    st.markdown(
        "<div class='ops-note'>React 是主工作台：提交文献、检索知识、审核候选和阅读报告。"
        "此页保留系统健康、队列和恢复入口，避免普通流程接触底层诊断。</div>",
        unsafe_allow_html=True,
    )
    st.link_button("打开 React 主工作台", REACT_WORKSPACE_URL, use_container_width=True)
    try:
        health = _api("GET", "/health")
        ingestions, _ = _ingestion_options()
        reports = _api("GET", "/api/reports")
    except httpx.HTTPError as exc:
        _show_api_error(exc)
        return

    services = health.get("services", {})
    knowledge = health.get("knowledge", {})
    jobs = knowledge.get("jobs", {})
    metrics = st.columns(4)
    metrics[0].metric("排队任务", jobs.get("queued", 0))
    metrics[1].metric("运行中", jobs.get("running", 0))
    waiting_review = sum(
        max(0, item.get("candidate_count", 0) - item.get("published_count", 0))
        for item in ingestions
    )
    metrics[2].metric("待审核候选", waiting_review)
    metrics[3].metric("失败报告", sum(item.get("status") == "failed" for item in reports))

    st.markdown("### 服务健康")
    service_columns = st.columns(max(1, len(services)))
    for column, (name, details) in zip(service_columns, services.items(), strict=False):
        available = bool(details.get("available"))
        if available:
            column.success(f"{name} · 可用")
        else:
            column.error(f"{name} · {details.get('detail') or '不可用'}")

    left, right = st.columns((1.2, 0.8), gap="large")
    with left:
        st.markdown("### 需要关注的任务")
        active = [
            item
            for item in ingestions
            if item.get("status") in {"queued", "running", "publishing", "failed", "interrupted"}
        ]
        if active:
            for item in active[:5]:
                _render_ingestion_status_card(item, key_prefix="ops")
        else:
            st.success("当前没有需要恢复或追踪的入库任务。")
    with right:
        st.markdown("### 运维边界")
        st.info(
            "审核中的 merge/link、失败重试和投影恢复应在本控制台完成；"
            "日常候选审核请使用 React 审核中心。"
        )
        st.caption(
            f"LLM：{knowledge.get('llm_provider', 'unknown')} · "
            f"{'已配置真实模型' if knowledge.get('live_llm_configured') else '未配置真实模型'}"
        )
        st.markdown("#### 最近报告")
        if reports:
            for report in reports[:4]:
                st.write(f"- **{report['status']}** · {report['query'][:48]}")
        else:
            st.caption("暂无报告任务。")


def _evidence(item: dict[str, Any]) -> None:
    evidence = item.get("evidence") or {}
    if not evidence:
        return
    pages: str | int | None = evidence.get("page_start")
    if evidence.get("page_end") and evidence.get("page_end") != pages:
        pages = f"{pages}-{evidence['page_end']}"
    st.caption(f"证据 · p. {pages} · {evidence.get('chunk_id', '')}")
    st.info(evidence.get("quote", evidence.get("text", "")))


def _render_knowledge_ingestion() -> None:
    st.subheader("知识入库")
    st.caption("只接收本地或远程 PDF。API 仅创建任务，持久 worker 完成解析、索引和候选抽取。")
    try:
        knowledge = _api("GET", "/health").get("knowledge", {})
        provider = knowledge.get("llm_provider", "未读取")
        enabled = knowledge.get("live_llm_configured", False)
        st.info(f"LLM：{provider} · {'可入库' if enabled else '未配置真实模型，提交会被阻止'}")
    except httpx.HTTPError:
        st.caption("无法读取服务状态；请确认 API 已启动。")

    with st.form("knowledge_ingestion_form"):
        collection = st.text_input(
            "知识集合（可选）",
            placeholder="留空进入“收件箱”，例如：LLM Agent 的工具使用与规划",
        )
        sources = st.text_area(
            "PDF 路径或 URL（每行一个）",
            placeholder="data/raw_papers/2302.04761.pdf\nhttps://arxiv.org/pdf/2302.04761",
            height=160,
        )
        max_pages = st.number_input("每篇 PDF 页数上限", 1, 150, 20)
        submitted = st.form_submit_button("提交入库任务", type="primary", use_container_width=True)
    submitted_ingestion: dict[str, Any] | None = None
    if submitted:
        try:
            with st.spinner("正在创建入库任务…"):
                submitted_ingestion = _api(
                    "POST",
                    "/api/knowledge/ingestions",
                    json={
                        "collection": collection or None,
                        "sources": [line.strip() for line in sources.splitlines() if line.strip()],
                        "pdf_max_pages": int(max_pages),
                    },
                )
            st.session_state.knowledge_ingestion_id = submitted_ingestion["id"]
            if submitted_ingestion.get("deduplicated"):
                st.warning(
                    "相同的任务已在处理中，未重复创建。以下展示现有任务的实时状态。"
                )
            else:
                st.success("任务已提交并持久化到队列。")
        except httpx.HTTPError as exc:
            _show_api_error(exc)
    if submitted_ingestion is not None:
        _render_ingestion_status_card(submitted_ingestion, key_prefix="submitted")
    elif latest_id := st.session_state.get("knowledge_ingestion_id"):
        try:
            latest = _api("GET", f"/api/knowledge/ingestions/{latest_id}")
            st.markdown("### 最近提交任务")
            _render_ingestion_status_card(latest, key_prefix="latest")
        except httpx.HTTPError as exc:
            _show_api_error(exc)


def _ingestion_options() -> tuple[list[dict[str, Any]], dict[str, str]]:
    ingestions = _api("GET", "/api/knowledge/ingestions")
    labels = {
        item["id"]: (
            f"{item['collection']} · {INGESTION_STATUS_LABELS.get(item['status'], item['status'])}"
            f" · {_format_beijing_time(item['created_at'])} 北京时间"
        )
        for item in ingestions
    }
    return ingestions, labels


def _render_review_queue() -> None:
    st.subheader("审核队列")
    st.caption("候选只能从 draft 进入一次终态；待定项保留论文内证据，但不会进入正式图谱。")
    try:
        ingestions, labels = _ingestion_options()
    except httpx.HTTPError as exc:
        _show_api_error(exc)
        return
    if not ingestions:
        st.info("暂无入库任务。")
        return
    default_id = st.session_state.get("knowledge_ingestion_id", ingestions[0]["id"])
    index = next((i for i, item in enumerate(ingestions) if item["id"] == default_id), 0)
    ingestion_id = st.selectbox(
        "选择任务",
        [item["id"] for item in ingestions],
        index=index,
        format_func=labels.get,
    )
    try:
        candidates = _api("GET", f"/api/knowledge/ingestions/{ingestion_id}/candidates")
    except httpx.HTTPError as exc:
        _show_api_error(exc)
        return
    drafts = [item for item in candidates if item["candidate"]["status"] == "draft"]
    stats = st.columns(4)
    stats[0].metric("候选总数", len(candidates))
    stats[1].metric("待审核", len(drafts))
    stats[2].metric("待审实体", sum(item["kind"] == "entity" for item in drafts))
    stats[3].metric("待审关系", sum(item["kind"] == "relation" for item in drafts))
    if drafts and st.button("批准所有无冲突候选", key=f"approve_ready_{ingestion_id}"):
        try:
            result = _api("POST", f"/api/knowledge/ingestions/{ingestion_id}/approve-ready")
            st.success(
                "已保存审核："
                f"实体 {result['published_entities']}、冲突 {result['skipped_conflicts']}、"
                f"需逐项审核关系 {result['blocked_relations']}。"
            )
            st.rerun()
        except httpx.HTTPError as exc:
            _show_api_error(exc)

    for heading, kind in (("实体候选", "entity"), ("关系候选", "relation")):
        st.markdown(f"### {heading}")
        items = [item for item in drafts if item["kind"] == kind]
        if not items:
            st.caption("无待审核项。")
        for item in items:
            _review_candidate(item, ingestion_id)


def _review_candidate(item: dict[str, Any], ingestion_id: str) -> None:
    candidate = item["candidate"]
    kind = item["kind"]
    label = candidate.get("name") or (
        f"{candidate.get('type')} · {candidate['source_candidate_id'][-8:]} → "
        f"{candidate['target_candidate_id'][-8:]}"
    )
    with st.expander(f"{label} · 置信度 {candidate['confidence']:.0%}"):
        with st.form(f"candidate_form_{candidate['id']}"):
            if kind == "entity":
                name = st.text_input("规范名称", value=candidate["name"])
                sense_qualifier = st.text_input(
                    "词义限定语（可选）", value=candidate.get("sense_qualifier", "")
                )
                aliases = st.text_input(
                    "别名（逗号分隔）", value=", ".join(candidate.get("aliases", []))
                )
            else:
                name = aliases = sense_qualifier = None
                st.code(
                    f"{candidate['source_candidate_id']} --{candidate['type']}--> "
                    f"{candidate['target_candidate_id']}"
                )
            summary = st.text_area("中文说明", value=candidate["summary"], height=100)
            confidence = st.slider("置信度", 0.0, 1.0, float(candidate["confidence"]), 0.01)
            save = st.form_submit_button("保存修改")
        if save:
            patch: dict[str, Any] = {"summary": summary, "confidence": confidence}
            if kind == "entity":
                patch.update(
                    {
                        "name": name,
                        "aliases": [value.strip() for value in aliases.split(",") if value.strip()],
                        "sense_qualifier": sense_qualifier,
                    }
                )
            try:
                _api("PATCH", f"/api/knowledge/candidates/{candidate['id']}", json=patch)
                st.rerun()
            except httpx.HTTPError as exc:
                _show_api_error(exc)
        _evidence(candidate)
        review_note = st.text_input("审核备注（可选）", key=f"review_note_{candidate['id']}")
        suggestions = candidate.get("merge_suggestions", []) if kind == "entity" else []
        if suggestions:
            target = st.selectbox(
                "合并到规范实体",
                suggestions,
                format_func=lambda value: (
                    f"{value['name']} · {value['type']} · {value['match_kind']} "
                    f"{value['similarity']:.0%}"
                ),
                key=f"merge_target_{candidate['id']}",
            )
            approve, merge, defer, reject = st.columns(4)
            if approve.button("作为新实体批准", key=f"approve_{candidate['id']}"):
                _decide_candidate(
                    candidate["id"],
                    {"decision": "approve", "review_note": review_note},
                    ingestion_id,
                )
            if merge.button("链接到已有词义", key=f"merge_{candidate['id']}"):
                _decide_candidate(
                    candidate["id"],
                    {
                        "decision": "link",
                        "canonical_id": target["entity_id"],
                        "review_note": review_note,
                    },
                    ingestion_id,
                )
            if defer.button("仅保留论文内提及", key=f"defer_{candidate['id']}"):
                _decide_candidate(
                    candidate["id"], {"decision": "defer", "review_note": review_note}, ingestion_id
                )
            if reject.button("驳回", key=f"reject_{candidate['id']}"):
                _decide_candidate(
                    candidate["id"],
                    {"decision": "reject", "review_note": review_note},
                    ingestion_id,
                )
        else:
            approve, defer, reject = st.columns(3)
            if approve.button("批准", key=f"approve_{candidate['id']}"):
                _decide_candidate(
                    candidate["id"],
                    {"decision": "approve", "review_note": review_note},
                    ingestion_id,
                )
            if defer.button("待定", key=f"defer_{candidate['id']}"):
                _decide_candidate(
                    candidate["id"], {"decision": "defer", "review_note": review_note}, ingestion_id
                )
            if reject.button("驳回", key=f"reject_{candidate['id']}"):
                _decide_candidate(
                    candidate["id"],
                    {"decision": "reject", "review_note": review_note},
                    ingestion_id,
                )


def _decide_candidate(candidate_id: str, payload: dict[str, str], ingestion_id: str) -> None:
    try:
        result = _api("POST", f"/api/knowledge/candidates/{candidate_id}/decision", json=payload)
        st.session_state.knowledge_ingestion_id = ingestion_id
        message = "审核决定已保存。" if result["applied"] else "相同审核决定已幂等重放。"
        st.success(f"{message} 投影状态：{result['projection_status']}。")
        st.rerun()
    except httpx.HTTPError as exc:
        _show_api_error(exc)


def _render_knowledge_explorer() -> None:
    st.subheader("知识探索")
    st.caption("这里只展示已审核的 SQLite 正式知识；Neo4j 和 Obsidian 是可恢复投影。")
    try:
        collections = _api("GET", "/api/knowledge/collections")
    except httpx.HTTPError as exc:
        _show_api_error(exc)
        return
    collection_by_slug = {item["slug"]: item["name"] for item in collections}
    collection_slug = st.selectbox(
        "知识集合",
        ["__all__", *collection_by_slug],
        format_func=lambda slug: "全部正式知识" if slug == "__all__" else collection_by_slug[slug],
    )
    try:
        data = _api(
            "GET",
            "/api/knowledge/graph",
            **(
                {}
                if collection_slug == "__all__"
                else {"params": {"collection_slug": collection_slug}}
            ),
        )
    except httpx.HTTPError as exc:
        _show_api_error(exc)
        return
    entities, relations = data.get("nodes", []), data.get("edges", [])
    st.info(f"已发布 {len(entities)} 个节点、{len(relations)} 条关系。")
    if not entities:
        return
    by_id = {item["id"]: item for item in entities}
    center_id = st.selectbox(
        "以节点为中心浏览",
        list(by_id),
        format_func=lambda item_id: (
            (
                f"{by_id[item_id]['name']}"
                f"（{by_id[item_id].get('metadata', {}).get('sense_qualifier')}）"
                if by_id[item_id].get("metadata", {}).get("sense_qualifier")
                else by_id[item_id]["name"]
            )
            + f" · {by_id[item_id]['type']}"
        ),
    )
    local_relations = [
        item
        for item in relations
        if center_id in {item["source_entity_id"], item["target_entity_id"]}
    ]
    local_ids = {center_id}
    for relation in local_relations:
        local_ids.update({relation["source_entity_id"], relation["target_entity_id"]})
    _graph_chart([by_id[item_id] for item_id in local_ids if item_id in by_id], local_relations)
    try:
        detail = _api("GET", f"/api/knowledge/entities/{center_id}")
    except httpx.HTTPError as exc:
        _show_api_error(exc)
        return
    selected = detail["entity"]
    qualifier = selected.get("metadata", {}).get("sense_qualifier", "")
    st.markdown(f"### {selected['name']}{f'（{qualifier}）' if qualifier else ''}")
    st.write(selected["summary"])
    sense = detail.get("concept_sense") or {}
    if sense.get("scope"):
        st.caption("词义语境：" + sense["scope"])
    if selected.get("aliases"):
        st.caption("别名：" + "、".join(selected["aliases"]))
    if detail.get("relations"):
        st.markdown("#### 关联知识")
        for relation in detail["relations"]:
            other_id = (
                relation["target_entity_id"]
                if relation["source_entity_id"] == center_id
                else relation["source_entity_id"]
            )
            other = by_id.get(other_id, {"name": other_id})
            st.write(f"- {relation['type']} · {other['name']}：{relation['summary']}")
    if detail.get("papers"):
        st.markdown("#### 来源论文阅读卡")
        for paper in detail["papers"]:
            reading = paper.get("metadata", {}).get("reading", {})
            if reading:
                st.write(
                    f"**{paper['name']}**：{reading.get('research_problem', paper['summary'])}"
                )
    st.markdown("#### 证据来源")
    for evidence in selected.get("evidence", []):
        _evidence({"evidence": evidence})


def _graph_chart(nodes: list[dict[str, Any]], edges: list[dict[str, Any]]) -> None:
    if not nodes:
        return
    dot = ["digraph KnowledgeGraph {", "rankdir=LR;", "node [shape=box style=rounded];"]
    node_ids = {node["id"] for node in nodes}
    for node in nodes:
        label = str(node.get("name", "")).replace('"', "'")[:60]
        dot.append(f'"{node["id"]}" [label="{label}\\n{node.get("type", "Concept")}"];')
    for edge in edges:
        source_id = edge.get("source_entity_id", edge.get("source_id"))
        target_id = edge.get("target_entity_id", edge.get("target_id"))
        if source_id in node_ids and target_id in node_ids:
            label = str(edge.get("type", edge.get("relation_type", "RELATED"))).replace('"', "'")
            dot.append(f'"{source_id}" -> "{target_id}" [label="{label}"];')
    dot.append("}")
    st.graphviz_chart("\n".join(dot), use_container_width=True)


def _render_reports() -> None:
    st.subheader("研究报告")
    st.caption("报告只消费已审核 PDF 切片和正式图谱；证据不足会明确失败。")
    try:
        collections = _api("GET", "/api/knowledge/collections")
    except httpx.HTTPError as exc:
        _show_api_error(exc)
        return
    collection_by_slug = {item["slug"]: item["name"] for item in collections}
    with st.form("report_form"):
        query = st.text_area("研究问题", height=100)
        collection_slugs = st.multiselect(
            "知识集合范围（留空表示全部正式知识）",
            list(collection_by_slug),
            format_func=collection_by_slug.get,
        )
        left, right = st.columns(2)
        top_k = left.number_input("证据切片数", 1, 30, 8)
        depth = right.selectbox("报告深度", ["brief", "standard", "deep"], index=1)
        submitted = st.form_submit_button("生成报告", type="primary", use_container_width=True)
    if submitted:
        try:
            report = _api(
                "POST",
                "/api/reports",
                json={
                    "query": query,
                    "collection_slugs": collection_slugs,
                    "top_k": int(top_k),
                    "report_depth": depth,
                },
            )
            st.session_state.report_id = report["id"]
            st.success(f"报告任务已排队：{report['id']}")
        except httpx.HTTPError as exc:
            _show_api_error(exc)
    try:
        reports = _api("GET", "/api/reports")
    except httpx.HTTPError as exc:
        _show_api_error(exc)
        return
    if not reports:
        return
    report_by_id = {item["id"]: item for item in reports}
    default_id = st.session_state.get("report_id", reports[0]["id"])
    selected_id = st.selectbox(
        "报告历史",
        list(report_by_id),
        index=list(report_by_id).index(default_id) if default_id in report_by_id else 0,
        format_func=lambda report_id: (
            f"{report_by_id[report_id]['query'][:60]} · {report_by_id[report_id]['status']}"
        ),
    )
    report = report_by_id[selected_id]
    st.info(f"状态：{report['status']} · ID：{report['id']}")
    if report["status"] == "failed":
        st.error(report.get("error") or "报告生成失败。")
    if report["status"] != "completed":
        if st.button("刷新报告状态"):
            st.rerun()
        return
    evaluation = report.get("evaluation") or {}
    metrics = st.columns(4)
    metrics[0].metric("证据落地", f"{evaluation.get('evidence_grounding', 0):.0%}")
    metrics[1].metric("引用覆盖", f"{evaluation.get('citation_coverage', 0):.0%}")
    metrics[2].metric("引用忠实", f"{evaluation.get('citation_fidelity', 0):.0%}")
    metrics[3].metric("结构", f"{evaluation.get('structure_score', 0):.0%}")
    st.download_button(
        "下载 Markdown",
        report["content"],
        file_name=f"{report['id']}.md",
        mime="text/markdown",
    )
    report_tab, evidence_tab = st.tabs(["报告", "证据包"])
    with report_tab:
        st.markdown(report["content"])
    with evidence_tab:
        for evidence in report.get("evidence", []):
            st.markdown(f"**[{evidence['id']}] {evidence['title']} · p.{evidence['page_start']}**")
            st.write(evidence["text"])


def _render_task_history() -> None:
    st.subheader("任务历史")
    st.caption(
        "所有时间均为北京时间；队列、租约、尝试次数和最近错误均保存在 SQLite，worker 重启可恢复。"
    )
    try:
        ingestions, labels = _ingestion_options()
        health = _api("GET", "/health").get("knowledge", {})
    except httpx.HTTPError as exc:
        _show_api_error(exc)
        return
    st.json({"jobs": health.get("jobs", {}), "projections": health.get("projections", {})})
    if not ingestions:
        st.info("暂无入库历史。")
        return
    st.dataframe(
        [
            {
                "知识集合": item["collection"],
                "状态": INGESTION_STATUS_LABELS.get(item["status"], item["status"]),
                "进度": _ingestion_progress(item)[1],
                "PDF": f"{item['document_count']} / {len(item['sources'])}",
                "候选": item["candidate_count"],
                "已发布": item["published_count"],
                "队列位置": (
                    f"第 {item['queue_position']} 位" if item.get("queue_position") else "—"
                ),
                "执行尝试": item.get("job_attempts", 0),
                "待投影": item["pending_projection_count"],
                "失败投影": item["failed_projection_count"],
                "提交时间（北京时间）": _format_beijing_time(item["created_at"]),
                "更新时间（北京时间）": _format_beijing_time(item["updated_at"]),
                "错误": item.get("error") or "",
            }
            for item in ingestions
        ],
        use_container_width=True,
        hide_index=True,
    )
    selected_id = st.selectbox(
        "查看或恢复任务",
        [item["id"] for item in ingestions],
        format_func=labels.get,
    )
    selected = next(item for item in ingestions if item["id"] == selected_id)
    st.markdown("### 任务详情")
    _render_ingestion_status_card(selected, key_prefix="history")
    try:
        collections = _api("GET", "/api/knowledge/collections")
    except httpx.HTTPError:
        collections = []
    if collections and selected["status"] not in {"queued", "running", "publishing"}:
        names = {item["slug"]: item["name"] for item in collections}
        target_slug = st.selectbox(
            "移动到知识集合",
            list(names),
            index=list(names).index(selected["collection_slug"])
            if selected["collection_slug"] in names
            else 0,
            format_func=names.get,
            key=f"move_collection_{selected_id}",
        )
        if target_slug != selected["collection_slug"] and st.button("移动集合"):
            try:
                _api(
                    "PATCH",
                    f"/api/knowledge/ingestions/{selected_id}/collection",
                    json={"collection": names[target_slug]},
                )
                st.success("集合移动已排队同步。")
                st.rerun()
            except httpx.HTTPError as exc:
                _show_api_error(exc)
    if selected["status"] in {"failed", "interrupted"} and st.button("重试此任务", type="primary"):
        try:
            _api("POST", f"/api/knowledge/ingestions/{selected_id}/retry")
            st.success("已重新排队。")
            st.rerun()
        except httpx.HTTPError as exc:
            _show_api_error(exc)


with st.sidebar:
    st.subheader("运营导航")
    page = st.radio("控制台页面", ["运营概览", "任务与恢复", "审核诊断", "知识诊断", "报告验收"])
    st.caption("主工作台：React · 诊断控制台：Streamlit")
    st.caption(f"API：{API_BASE_URL}")

if page == "运营概览":
    _render_operations_overview()
elif page == "任务与恢复":
    _render_task_history()
elif page == "审核诊断":
    _render_review_queue()
elif page == "知识诊断":
    _render_knowledge_explorer()
else:
    _render_reports()
