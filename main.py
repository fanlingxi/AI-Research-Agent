from __future__ import annotations

import typer
from rich.console import Console
from rich.panel import Panel

from app.tools.pdf_tools import parse_pdf_source
from app.worker import build_worker

cli = typer.Typer(help="Research Knowledge Core 本地工具")
console = Console()


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


@cli.command()
def worker() -> None:
    """启动本地持久任务与正式投影 worker。"""

    instance = build_worker()
    instance.run_forever(instance.ingestion_service.settings.knowledge_worker_poll_seconds)


if __name__ == "__main__":
    cli()
