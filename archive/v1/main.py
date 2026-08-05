from __future__ import annotations

import typer
from rich.console import Console
from rich.panel import Panel

from app.graph.workflow import run_research_workflow
from app.tools.pdf_tools import parse_pdf_source

cli = typer.Typer(help="AI Research Agent with GraphRAG")
console = Console()
PDF_OPTION = typer.Option(
    (),
    "--pdf",
    help="要纳入本次研究的本地或远程 PDF；可重复传入该选项。",
)

TRACE_LABELS = {
    "memory_context": "长期记忆召回",
    "planner": "任务规划",
    "tool_executor": "工具调用",
    "search": "资料检索",
    "document": "文档处理",
    "knowledge": "知识图谱",
    "retrieval": "向量检索",
    "graph_reasoning": "图谱推理",
    "synthesis": "报告生成",
    "critic": "质量审查",
    "reflection": "反思修订",
    "obsidian_export": "Obsidian 导出",
    "memory_write": "长期记忆写入",
}


@cli.command()
def run(
    query: str,
    live_search: bool = typer.Option(
        False,
        "--live-search/--offline",
        help="启用 arXiv 在线检索；离线模式使用确定性的演示论文。",
    ),
    paper_limit: int = typer.Option(5, help="候选论文数量。"),
    top_k: int = typer.Option(5, help="返回的检索切片数量。"),
    vector_store: str = typer.Option(
        "memory",
        help="向量库提供方：memory 或 qdrant。",
    ),
    graph_store: str = typer.Option(
        "memory",
        help="图数据库提供方：memory 或 neo4j。",
    ),
    memory: bool = typer.Option(
        True,
        "--memory/--no-memory",
        help="是否启用长期记忆。",
    ),
    pdf: list[str] = PDF_OPTION,
    pdf_max_pages: int = typer.Option(
        12,
        min=1,
        help="每篇显式 PDF 最多解析的页数。",
    ),
    export_obsidian: bool = typer.Option(
        False,
        "--export-obsidian/--no-export-obsidian",
        help="仅在真实证据门槛通过后导出 Obsidian Vault。",
    ),
    obsidian_vault: str | None = typer.Option(
        None,
        "--obsidian-vault",
        help="Obsidian Vault 路径，默认读取 OBSIDIAN_VAULT_PATH。",
    ),
) -> None:
    """运行带可信证据门槛与可选 Obsidian 导出的 GraphRAG 研究流程。"""

    result = run_research_workflow(
        query=query,
        live_search=live_search,
        paper_limit=paper_limit,
        top_k=top_k,
        vector_store_provider=vector_store,
        graph_store_provider=graph_store,
        memory_enabled=memory,
        document_sources=pdf or None,
        pdf_max_pages=pdf_max_pages,
        obsidian_export_enabled=export_obsidian,
        obsidian_vault_path=obsidian_vault,
    )
    console.print(Panel(result["final_report"], title="AI-Research-Agent"))

    traces = result.get("traces", [])
    if traces:
        console.print("\n[bold]执行轨迹[/bold]")
        for trace in traces:
            label = TRACE_LABELS.get(trace.node, trace.node)
            console.print(f"- {label}: {trace.message}")


@cli.command("parse-pdf")
def parse_pdf(source: str, max_pages: int | None = None) -> None:
    """解析本地或远程 PDF，并打印抽取元信息。"""

    parsed = parse_pdf_source(source=source, max_pages=max_pages)
    console.print(
        Panel(
            f"来源：{parsed.source}\n标题：{parsed.title}\n页数：{parsed.pages}\n"
            f"字符数：{len(parsed.text)}",
            title="PDF 解析结果",
        )
    )


if __name__ == "__main__":
    cli()
