from __future__ import annotations

import os
import time

import httpx
import streamlit as st

API_BASE_URL = os.getenv("AI_RESEARCH_API_URL", "http://localhost:8000")

st.set_page_config(page_title="AI-Research-Agent", page_icon="R", layout="wide")
st.title("AI-Research-Agent")
st.caption("Evidence-governed Agentic GraphRAG research workspace")


def _api(method: str, path: str, **kwargs):
    response = httpx.request(method, f"{API_BASE_URL}{path}", timeout=120.0, **kwargs)
    response.raise_for_status()
    return response.json()


with st.sidebar:
    st.subheader("运行设置")
    live_search = st.toggle("在线 arXiv 检索", value=False)
    memory_enabled = st.toggle("长期记忆", value=True)
    export_obsidian = st.toggle("导出 Obsidian Vault", value=False)
    vault_path = st.text_input("Vault 路径", value="data/obsidian_vault")
    paper_limit = st.number_input("候选论文数", min_value=1, max_value=20, value=5)
    top_k = st.number_input("检索证据数", min_value=1, max_value=20, value=5)
    pdf_max_pages = st.number_input("每篇 PDF 页数上限", min_value=1, max_value=100, value=12)
    vector_store = st.selectbox("向量后端", ["memory", "qdrant"])
    graph_store = st.selectbox("图谱后端", ["memory", "neo4j"])

query = st.text_area(
    "研究主题",
    value="GraphRAG 如何支持科研文献综述？",
    height=100,
)
pdf_sources = st.text_area(
    "PDF 来源（每行一个本地路径或 URL）",
    placeholder="data/raw_papers/2404-16130.pdf",
    height=100,
)

if st.button("开始研究", type="primary", use_container_width=True):
    payload = {
        "query": query,
        "live_search": live_search,
        "paper_limit": int(paper_limit),
        "top_k": int(top_k),
        "vector_store_provider": vector_store,
        "graph_store_provider": graph_store,
        "memory_enabled": memory_enabled,
        "document_sources": [line.strip() for line in pdf_sources.splitlines() if line.strip()],
        "pdf_max_pages": int(pdf_max_pages),
        "obsidian_export_enabled": export_obsidian,
        "obsidian_vault_path": vault_path or None,
    }
    try:
        st.session_state.task = _api("POST", "/api/research", json=payload)
    except httpx.HTTPError as exc:
        st.error(f"无法连接 API：{exc}")

task = st.session_state.get("task")
if task:
    try:
        task = _api("GET", f"/api/tasks/{task['id']}")
        st.session_state.task = task
    except httpx.HTTPError as exc:
        st.error(f"读取任务状态失败：{exc}")
        task = None

if task:
    status_column, refresh_column = st.columns([4, 1])
    status_column.info(f"任务状态：{task['status']} | 任务 ID：{task['id']}")
    if refresh_column.button("刷新", use_container_width=True):
        time.sleep(0.3)
        st.rerun()

    if task["status"] == "completed":
        report = _api("GET", f"/api/tasks/{task['id']}/report")["report"]
        graph = _api("GET", f"/api/tasks/{task['id']}/graph")
        evaluation = task.get("evaluation") or {}
        metrics = evaluation.get("metrics", [])

        overview, evidence, graph_tab, report_tab = st.tabs(
            ["质量概览", "证据", "关系图谱", "研究报告"]
        )
        with overview:
            left, middle, right = st.columns(3)
            left.metric("综合评分", f"{evaluation.get('overall_score', 0):.2f}")
            middle.metric("正式沉淀", "允许" if evaluation.get("evidence_admissible") else "阻止")
            right.metric("证据状态", evaluation.get("evidence_status", "unknown"))
            st.dataframe(metrics, use_container_width=True, hide_index=True)
            st.json(task.get("obsidian_export") or {"exported": False})
        with evidence:
            st.caption("正式报告只应使用可追溯的真实来源证据。")
            st.markdown(report.split("## 知识图谱")[0])
        with graph_tab:
            selected_types = st.multiselect(
                "显示节点类型",
                options=sorted({node.get("type", "Concept") for node in graph["nodes"]}),
                default=sorted({node.get("type", "Concept") for node in graph["nodes"]}),
            )
            nodes = [node for node in graph["nodes"] if node.get("type") in selected_types]
            node_ids = {node["id"] for node in nodes}
            dot = ["digraph ResearchGraph {", "rankdir=LR;", "node [shape=box];"]
            for node in nodes:
                label = str(node.get("name", "")).replace('"', "'")[:60]
                dot.append(f'"{node["id"]}" [label="{label}"];')
            for edge in graph["edges"]:
                if edge["source_id"] in node_ids and edge["target_id"] in node_ids:
                    label = str(edge.get("type", "RELATED")).replace('"', "'")
                    dot.append(
                        f'"{edge["source_id"]}" -> "{edge["target_id"]}" [label="{label}"];'
                    )
            dot.append("}")
            st.graphviz_chart("\n".join(dot), use_container_width=True)
        with report_tab:
            st.download_button("下载 Markdown 报告", report, file_name="research_report.md")
            st.markdown(report)
    elif task["status"] == "failed":
        st.error(task.get("error") or "任务执行失败。")
