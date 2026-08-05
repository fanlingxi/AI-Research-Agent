#!/usr/bin/env python3
"""Interactive API client for manual Knowledge Core acceptance testing.

The client is read-only unless a mutating subcommand is explicitly selected.
Review decisions and bulk approval require an additional YES confirmation unless
--yes is passed.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

import httpx


class ManualTestError(RuntimeError):
    """A readable acceptance-test failure."""


class ManualApi:
    def __init__(self, base_url: str, *, timeout: float = 60.0) -> None:
        self.client = httpx.Client(base_url=base_url.rstrip("/"), timeout=timeout)

    def close(self) -> None:
        self.client.close()

    def request(self, method: str, path: str, **kwargs: Any) -> Any:
        try:
            response = self.client.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise ManualTestError(f"无法连接 API：{exc}") from exc
        if response.is_error:
            try:
                detail = response.json().get("detail", response.text)
            except (ValueError, AttributeError):
                detail = response.text
            raise ManualTestError(f"{method} {path} -> {response.status_code}: {detail}")
        if response.status_code == 204 or not response.content:
            return None
        content_type = response.headers.get("content-type", "")
        return response.json() if "json" in content_type else response.text

    def status_code(self, method: str, path: str, **kwargs: Any) -> int:
        try:
            return self.client.request(method, path, **kwargs).status_code
        except httpx.HTTPError as exc:
            raise ManualTestError(f"无法连接 API：{exc}") from exc


def _print_json(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2))


def _confirm(message: str, *, assume_yes: bool) -> None:
    if assume_yes:
        return
    answer = input(f"{message}\n输入 YES 继续：").strip()
    if answer != "YES":
        raise ManualTestError("操作已取消，未写入修改或审核决定。")


def _wait_for(
    api: ManualApi,
    path: str,
    *,
    accepted: set[str],
    failed: set[str],
    timeout: float,
    interval: float,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    last_status = ""
    while time.monotonic() < deadline:
        resource = api.request("GET", path)
        status = resource["status"]
        if status != last_status:
            print(f"状态：{status}")
            last_status = status
        if status in accepted:
            return resource
        if status in failed:
            raise ManualTestError(f"资源进入 {status}：{resource.get('error') or '无错误详情'}")
        time.sleep(interval)
    raise ManualTestError(f"等待超时（{timeout:.0f}s），最后状态：{last_status or 'unknown'}")


def _candidate_summary(items: list[dict[str, Any]]) -> dict[str, Any]:
    rows = []
    for item in items:
        candidate = item["candidate"]
        evidence = candidate.get("evidence") or {}
        row = {
            "kind": item["kind"],
            "id": candidate["id"],
            "status": candidate["status"],
            "type": candidate.get("type"),
            "name": candidate.get("name"),
            "confidence": candidate.get("confidence"),
            "page": evidence.get("page_start"),
            "quote": evidence.get("quote"),
        }
        if item["kind"] == "relation":
            row["source_candidate_id"] = candidate.get("source_candidate_id")
            row["target_candidate_id"] = candidate.get("target_candidate_id")
        if candidate.get("merge_suggestions"):
            row["merge_suggestions"] = candidate["merge_suggestions"]
        rows.append(row)
    return {
        "total": len(rows),
        "draft": sum(row["status"] == "draft" for row in rows),
        "entities": sum(row["kind"] == "entity" for row in rows),
        "relations": sum(row["kind"] == "relation" for row in rows),
        "candidates": rows,
    }


def _report_quality_errors(report: dict[str, Any]) -> list[str]:
    evaluation = report.get("evaluation") or {}
    errors = []
    if report.get("status") != "completed":
        errors.append(f"report status is {report.get('status')}")
    if evaluation.get("evidence_grounding") != 1.0:
        errors.append("evidence_grounding must equal 1.0")
    if float(evaluation.get("citation_coverage", 0.0)) < 0.9:
        errors.append("citation_coverage must be at least 0.9")
    if evaluation.get("citation_fidelity") != 1.0:
        errors.append("citation_fidelity must equal 1.0")
    if float(evaluation.get("structure_score", 0.0)) < 0.8:
        errors.append("structure_score must be at least 0.8")
    return errors


def _save_report(api: ManualApi, report: dict[str, Any], output_dir: str) -> None:
    if not output_dir:
        return
    directory = Path(output_dir).expanduser().resolve()
    directory.mkdir(parents=True, exist_ok=True)
    report_id = report["id"]
    evidence = api.request("GET", f"/api/reports/{report_id}/evidence")
    markdown = api.request("GET", f"/api/reports/{report_id}/download")
    (directory / f"{report_id}.md").write_text(markdown, encoding="utf-8")
    (directory / f"{report_id}.evidence.json").write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    (directory / f"{report_id}.metadata.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"报告产物已保存：{directory}")


def _health(api: ManualApi, args: argparse.Namespace) -> None:
    health = api.request("GET", "/health")
    knowledge = health.get("knowledge", {})
    checks = {
        "status_ok": health.get("status") == "ok",
        "schema_at_least_v4": int(knowledge.get("schema_version", 0)) >= 4,
        "live_llm_configured": bool(knowledge.get("live_llm_configured")),
        "qdrant_available": bool(health.get("services", {}).get("qdrant", {}).get("available")),
        "neo4j_available": bool(health.get("services", {}).get("neo4j", {}).get("available")),
    }
    if args.check_legacy:
        checks["legacy_research_404"] = (
            api.status_code("POST", "/api/research", json={"query": "manual-check"}) == 404
        )
        checks["legacy_tasks_404"] = api.status_code("GET", "/api/tasks/manual-check") == 404
    _print_json({"checks": checks, "health": health})
    failed = [name for name, passed in checks.items() if not passed]
    if failed:
        raise ManualTestError(f"健康验收未通过：{', '.join(failed)}")


def _ingest(api: ManualApi, args: argparse.Namespace) -> None:
    ingestion = api.request(
        "POST",
        "/api/knowledge/ingestions",
        json={"topic": args.topic, "sources": args.pdf, "pdf_max_pages": args.max_pages},
    )
    print(f"INGESTION_ID={ingestion['id']}")
    _print_json(ingestion)
    if args.wait:
        completed = _wait_for(
            api,
            f"/api/knowledge/ingestions/{ingestion['id']}",
            accepted={"needs_review", "completed"},
            failed={"failed", "interrupted"},
            timeout=args.timeout,
            interval=args.interval,
        )
        _print_json(completed)


def _watch_ingestion(api: ManualApi, args: argparse.Namespace) -> None:
    result = _wait_for(
        api,
        f"/api/knowledge/ingestions/{args.ingestion_id}",
        accepted=set(args.until),
        failed={"failed", "interrupted"} - set(args.until),
        timeout=args.timeout,
        interval=args.interval,
    )
    _print_json(result)


def _candidates(api: ManualApi, args: argparse.Namespace) -> None:
    params = {"status": args.status} if args.status else None
    items = api.request(
        "GET",
        f"/api/knowledge/ingestions/{args.ingestion_id}/candidates",
        params=params,
    )
    _print_json(_candidate_summary(items))


def _patch_candidate(api: ManualApi, args: argparse.Namespace) -> None:
    payload: dict[str, Any] = {}
    if args.name is not None:
        payload["name"] = args.name
    if args.summary is not None:
        payload["summary"] = args.summary
    if args.aliases is not None:
        payload["aliases"] = [value.strip() for value in args.aliases.split(",") if value.strip()]
    if args.confidence is not None:
        payload["confidence"] = args.confidence
    if not payload:
        raise ManualTestError("至少提供一个待修改字段。")
    _confirm(f"将修改候选 {args.candidate_id}：{payload}", assume_yes=args.yes)
    _print_json(
        api.request("PATCH", f"/api/knowledge/candidates/{args.candidate_id}", json=payload)
    )


def _decide(api: ManualApi, args: argparse.Namespace) -> None:
    payload = {"decision": args.decision}
    if args.decision == "merge":
        if not args.canonical_id:
            raise ManualTestError("merge 必须提供 --canonical-id。")
        payload["canonical_id"] = args.canonical_id
    _confirm(f"将对候选 {args.candidate_id} 写入决定：{payload}", assume_yes=args.yes)
    _print_json(
        api.request(
            "POST",
            f"/api/knowledge/candidates/{args.candidate_id}/decision",
            json=payload,
        )
    )


def _approve_ready(api: ManualApi, args: argparse.Namespace) -> None:
    _confirm(
        f"将批量批准任务 {args.ingestion_id} 中所有无冲突候选。",
        assume_yes=args.yes,
    )
    _print_json(api.request("POST", f"/api/knowledge/ingestions/{args.ingestion_id}/approve-ready"))


def _retry_ingestion(api: ManualApi, args: argparse.Namespace) -> None:
    _print_json(api.request("POST", f"/api/knowledge/ingestions/{args.ingestion_id}/retry"))


def _topic(api: ManualApi, args: argparse.Namespace) -> None:
    _print_json(api.request("GET", f"/api/knowledge/topics/{args.topic_slug}"))


def _search(api: ManualApi, args: argparse.Namespace) -> None:
    params: list[tuple[str, str | int]] = [("q", args.query), ("top_k", args.top_k)]
    params.extend(("topic_slug", slug) for slug in args.topic_slug)
    result = api.request("GET", "/api/knowledge/search", params=params)
    _print_json(result)
    evidence = result.get("evidence", [])
    if not evidence:
        raise ManualTestError("正式检索没有返回证据。")
    invalid = [
        item.get("id", "unknown")
        for item in evidence
        if not item.get("paper_id")
        or not item.get("chunk_id")
        or int(item.get("page_start", 0)) < 1
        or not str(item.get("text", "")).strip()
    ]
    if invalid:
        raise ManualTestError(f"发现未落地证据：{', '.join(invalid)}")


def _report(api: ManualApi, args: argparse.Namespace) -> None:
    report = api.request(
        "POST",
        "/api/reports",
        json={
            "query": args.query,
            "topic_slugs": args.topic_slug,
            "top_k": args.top_k,
            "report_depth": args.depth,
        },
    )
    print(f"REPORT_ID={report['id']}")
    _print_json(report)
    if args.wait:
        report = _wait_for(
            api,
            f"/api/reports/{report['id']}",
            accepted={"completed"},
            failed={"failed"},
            timeout=args.timeout,
            interval=args.interval,
        )
        _print_json(report)
        errors = _report_quality_errors(report)
        if errors:
            raise ManualTestError("报告质量门未通过：" + "; ".join(errors))
        _save_report(api, report, args.output_dir)


def _watch_report(api: ManualApi, args: argparse.Namespace) -> None:
    report = _wait_for(
        api,
        f"/api/reports/{args.report_id}",
        accepted={"completed"},
        failed={"failed"},
        timeout=args.timeout,
        interval=args.interval,
    )
    _print_json(report)
    errors = _report_quality_errors(report)
    if errors:
        raise ManualTestError("报告质量门未通过：" + "; ".join(errors))
    _save_report(api, report, args.output_dir)


def _add_wait_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--timeout", type=float, default=600.0, help="最长等待秒数")
    parser.add_argument("--interval", type=float, default=2.0, help="轮询间隔秒数")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Research Knowledge Core 人工验收客户端")
    parser.add_argument("--base-url", default="http://localhost:8000", help="API 根地址")
    subparsers = parser.add_subparsers(dest="command", required=True)

    health = subparsers.add_parser("health", help="检查依赖、Schema、LLM 和旧接口隔离")
    health.add_argument("--check-legacy", action="store_true")
    health.set_defaults(handler=_health)

    ingest = subparsers.add_parser("ingest", help="提交真实 PDF 入库任务")
    ingest.add_argument("--topic", required=True)
    ingest.add_argument("--pdf", action="append", required=True, help="可重复传入路径或 URL")
    ingest.add_argument("--max-pages", type=int, default=20)
    ingest.add_argument("--wait", action="store_true")
    _add_wait_options(ingest)
    ingest.set_defaults(handler=_ingest)

    watch_ingestion = subparsers.add_parser("watch-ingestion", help="等待入库状态")
    watch_ingestion.add_argument("ingestion_id")
    watch_ingestion.add_argument(
        "--until",
        action="append",
        choices=[
            "queued",
            "running",
            "needs_review",
            "publishing",
            "completed",
            "failed",
            "interrupted",
        ],
        default=None,
        help="可重复；默认 completed",
    )
    _add_wait_options(watch_ingestion)
    watch_ingestion.set_defaults(handler=_watch_ingestion)

    candidates = subparsers.add_parser("candidates", help="列出候选和证据摘要")
    candidates.add_argument("ingestion_id")
    candidates.add_argument(
        "--status", choices=["draft", "approved", "rejected", "merged", "published"]
    )
    candidates.set_defaults(handler=_candidates)

    patch_candidate = subparsers.add_parser("patch-candidate", help="人工修订候选")
    patch_candidate.add_argument("candidate_id")
    patch_candidate.add_argument("--name")
    patch_candidate.add_argument("--summary")
    patch_candidate.add_argument("--aliases", help="英文逗号分隔")
    patch_candidate.add_argument("--confidence", type=float)
    patch_candidate.add_argument("--yes", action="store_true")
    patch_candidate.set_defaults(handler=_patch_candidate)

    decide = subparsers.add_parser("decide", help="批准、驳回或合并单个候选")
    decide.add_argument("candidate_id")
    decide.add_argument("decision", choices=["approve", "reject", "merge"])
    decide.add_argument("--canonical-id")
    decide.add_argument("--yes", action="store_true")
    decide.set_defaults(handler=_decide)

    approve = subparsers.add_parser("approve-ready", help="批量批准无冲突候选")
    approve.add_argument("ingestion_id")
    approve.add_argument("--yes", action="store_true")
    approve.set_defaults(handler=_approve_ready)

    retry = subparsers.add_parser("retry-ingestion", help="重新排队失败或中断任务")
    retry.add_argument("ingestion_id")
    retry.set_defaults(handler=_retry_ingestion)

    topic = subparsers.add_parser("topic", help="读取正式主题知识")
    topic.add_argument("topic_slug")
    topic.set_defaults(handler=_topic)

    search = subparsers.add_parser("search", help="验证正式知识检索和证据落地")
    search.add_argument("--query", required=True)
    search.add_argument("--topic-slug", action="append", default=[])
    search.add_argument("--top-k", type=int, default=5)
    search.set_defaults(handler=_search)

    report = subparsers.add_parser("report", help="创建报告并可等待质量验收")
    report.add_argument("--query", required=True)
    report.add_argument("--topic-slug", action="append", default=[])
    report.add_argument("--top-k", type=int, default=5)
    report.add_argument("--depth", choices=["brief", "standard", "deep"], default="standard")
    report.add_argument("--wait", action="store_true")
    report.add_argument("--output-dir", default="")
    _add_wait_options(report)
    report.set_defaults(handler=_report)

    watch_report = subparsers.add_parser("watch-report", help="等待已有报告并保存产物")
    watch_report.add_argument("report_id")
    watch_report.add_argument("--output-dir", default="")
    _add_wait_options(watch_report)
    watch_report.set_defaults(handler=_watch_report)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "watch-ingestion" and not args.until:
        args.until = ["completed"]
    api = ManualApi(args.base_url)
    try:
        args.handler(api, args)
    except ManualTestError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1
    finally:
        api.close()
    print("PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
