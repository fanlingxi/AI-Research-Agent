from __future__ import annotations

import typer
from rich.console import Console
from rich.panel import Panel

from app.graph.workflow import run_research_workflow
from app.tools.pdf_tools import parse_pdf_source

cli = typer.Typer(help="AI Research Agent with GraphRAG")
console = Console()


@cli.command()
def run(
    query: str,
    live_search: bool = typer.Option(
        False,
        "--live-search/--offline",
        help="Use live arXiv search. Offline mode uses deterministic demo papers.",
    ),
    paper_limit: int = typer.Option(5, help="Number of candidate papers to collect."),
    top_k: int = typer.Option(5, help="Number of retrieval chunks to return."),
    vector_store: str = typer.Option(
        "memory",
        help="Vector store provider: memory or qdrant.",
    ),
    graph_store: str = typer.Option(
        "memory",
        help="Graph store provider: memory or neo4j.",
    ),
) -> None:
    """Run the Phase 3 GraphRAG research pipeline."""

    result = run_research_workflow(
        query=query,
        live_search=live_search,
        paper_limit=paper_limit,
        top_k=top_k,
        vector_store_provider=vector_store,
        graph_store_provider=graph_store,
    )
    console.print(Panel(result["final_report"], title="AI-Research-Agent"))

    traces = result.get("traces", [])
    if traces:
        console.print("\n[bold]Execution Trace[/bold]")
        for trace in traces:
            console.print(f"- {trace.node}: {trace.message}")


@cli.command("parse-pdf")
def parse_pdf(source: str, max_pages: int | None = None) -> None:
    """Parse a local or remote PDF and print extraction metadata."""

    parsed = parse_pdf_source(source=source, max_pages=max_pages)
    console.print(
        Panel(
            f"Source: {parsed.source}\nTitle: {parsed.title}\nPages: {parsed.pages}\n"
            f"Characters: {len(parsed.text)}",
            title="PDF Parsed",
        )
    )


if __name__ == "__main__":
    cli()
