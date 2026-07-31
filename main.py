from __future__ import annotations

import typer
from rich.console import Console
from rich.panel import Panel

from app.graph.workflow import run_research_workflow

cli = typer.Typer(help="AI Research Agent with GraphRAG")
console = Console()


@cli.command()
def run(query: str) -> None:
    """Run the Phase 1 research workflow."""

    result = run_research_workflow(query)
    console.print(Panel(result["final_report"], title="AI-Research-Agent"))

    traces = result.get("traces", [])
    if traces:
        console.print("\n[bold]Execution Trace[/bold]")
        for trace in traces:
            console.print(f"- {trace.node}: {trace.message}")


if __name__ == "__main__":
    cli()
