from __future__ import annotations

import os
from typing import Any

import httpx
import streamlit as st

API_BASE_URL = os.getenv("AI_RESEARCH_API_URL", "http://localhost:8000")

st.set_page_config(page_title="Research Knowledge Core", page_icon="R", layout="wide")
st.title("Research Knowledge Core")
st.caption("PDF 入库 · 人工审核 · 可靠投影 · 有据可查的研究报告")


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
        topic = st.text_input("主题", placeholder="例如：LLM Agent 的工具使用与规划")
        sources = st.text_area(
            "PDF 路径或 URL（每行一个）",
            placeholder="data/raw_papers/2302.04761.pdf\nhttps://arxiv.org/pdf/2302.04761",
            height=160,
        )
        max_pages = st.number_input("每篇 PDF 页数上限", 1, 100, 20)
        submitted = st.form_submit_button("提交入库任务", type="primary", use_container_width=True)
    if submitted:
        try:
            ingestion = _api(
                "POST",
                "/api/knowledge/ingestions",
                json={
                    "topic": topic,
                    "sources": [line.strip() for line in sources.splitlines() if line.strip()],
                    "pdf_max_pages": int(max_pages),
                },
            )
            st.session_state.knowledge_ingestion_id = ingestion["id"]
            st.success(f"已排队：{ingestion['id']}。可在任务历史查看 worker 进度。")
        except httpx.HTTPError as exc:
            _show_api_error(exc)


def _ingestion_options() -> tuple[list[dict[str, Any]], dict[str, str]]:
    ingestions = _api("GET", "/api/knowledge/ingestions")
    labels = {
        item["id"]: f"{item['topic']} · {item['status']} · {item['created_at'][:19]}"
        for item in ingestions
    }
    return ingestions, labels


def _render_review_queue() -> None:
    st.subheader("审核队列")
    st.caption("候选只能从 draft 进入一次终态；发布事实先落 SQLite，再由 outbox 投影。")
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
                f"实体 {result['published_entities']}、关系 {result['published_relations']}、"
                f"冲突 {result['skipped_conflicts']}、阻塞关系 {result['blocked_relations']}。"
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
                aliases = st.text_input(
                    "别名（逗号分隔）", value=", ".join(candidate.get("aliases", []))
                )
            else:
                name = aliases = None
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
                    }
                )
            try:
                _api("PATCH", f"/api/knowledge/candidates/{candidate['id']}", json=patch)
                st.rerun()
            except httpx.HTTPError as exc:
                _show_api_error(exc)
        _evidence(candidate)
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
            approve, merge, reject = st.columns(3)
            if approve.button("作为新实体批准", key=f"approve_{candidate['id']}"):
                _decide_candidate(candidate["id"], {"decision": "approve"}, ingestion_id)
            if merge.button("确认合并", key=f"merge_{candidate['id']}"):
                _decide_candidate(
                    candidate["id"],
                    {"decision": "merge", "canonical_id": target["entity_id"]},
                    ingestion_id,
                )
            if reject.button("驳回", key=f"reject_{candidate['id']}"):
                _decide_candidate(candidate["id"], {"decision": "reject"}, ingestion_id)
        else:
            approve, reject = st.columns(2)
            if approve.button("批准", key=f"approve_{candidate['id']}"):
                _decide_candidate(candidate["id"], {"decision": "approve"}, ingestion_id)
            if reject.button("驳回", key=f"reject_{candidate['id']}"):
                _decide_candidate(candidate["id"], {"decision": "reject"}, ingestion_id)


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
        topics = _api("GET", "/api/knowledge/topics")
    except httpx.HTTPError as exc:
        _show_api_error(exc)
        return
    if not topics:
        st.info("暂无主题。")
        return
    topic_slug = st.selectbox(
        "主题",
        [item["topic_slug"] for item in topics],
        format_func=lambda slug: next(
            item["topic"] for item in topics if item["topic_slug"] == slug
        ),
    )
    try:
        data = _api("GET", f"/api/knowledge/topics/{topic_slug}")
    except httpx.HTTPError as exc:
        _show_api_error(exc)
        return
    entities, relations = data.get("entities", []), data.get("relations", [])
    st.info(f"已发布 {len(entities)} 个节点、{len(relations)} 条关系。")
    if not entities:
        return
    by_id = {item["id"]: item for item in entities}
    center_id = st.selectbox(
        "以节点为中心浏览",
        list(by_id),
        format_func=lambda item_id: f"{by_id[item_id]['name']} · {by_id[item_id]['type']}",
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
    selected = by_id[center_id]
    st.markdown(f"### {selected['name']}")
    st.write(selected["summary"])
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
        topics = _api("GET", "/api/knowledge/topics")
    except httpx.HTTPError as exc:
        _show_api_error(exc)
        return
    topic_by_slug = {item["topic_slug"]: item["topic"] for item in topics}
    with st.form("report_form"):
        query = st.text_area("研究问题", height=100)
        topic_slugs = st.multiselect(
            "主题范围（留空表示全部正式知识）",
            list(topic_by_slug),
            format_func=topic_by_slug.get,
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
                    "topic_slugs": topic_slugs,
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
    st.caption("队列、租约、尝试次数和最近错误均保存在 SQLite，worker 重启可恢复。")
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
                "主题": item["topic"],
                "状态": item["status"],
                "文档": item["document_count"],
                "候选": item["candidate_count"],
                "已发布": item["published_count"],
                "执行中任务": item["queued_job_count"],
                "待投影": item["pending_projection_count"],
                "失败投影": item["failed_projection_count"],
                "更新时间": item["updated_at"],
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
    st.json(selected)
    if selected["status"] in {"failed", "interrupted"} and st.button("重试此任务", type="primary"):
        try:
            _api("POST", f"/api/knowledge/ingestions/{selected_id}/retry")
            st.success("已重新排队。")
            st.rerun()
        except httpx.HTTPError as exc:
            _show_api_error(exc)


with st.sidebar:
    st.subheader("工作区")
    page = st.radio("页面", ["知识入库", "审核队列", "知识探索", "研究报告", "任务历史"])
    st.caption(f"API：{API_BASE_URL}")

if page == "知识入库":
    _render_knowledge_ingestion()
elif page == "审核队列":
    _render_review_queue()
elif page == "知识探索":
    _render_knowledge_explorer()
elif page == "研究报告":
    _render_reports()
else:
    _render_task_history()
