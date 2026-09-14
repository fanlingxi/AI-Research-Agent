"""Read-only export of the frozen closeout run; never edits human labels."""

import argparse
from collections import Counter
from pathlib import Path

from app.benchmarking.resume_closeout import OUTPUT, ROOT, read, write


def export(output):
    output = Path(output).resolve(strict=True)
    if output.parent != OUTPUT.resolve():
        raise ValueError("Export requires a managed closeout archive")
    config, rows = read(output / "config.json"), read(output / "results.json")
    if any(r["status"] in {"not_attempted", "queued"} for r in rows):
        raise ValueError("Wait for all cases before exporting")
    destination = ROOT / "docs/improvement/resume-closeout"
    destination.mkdir(exist_ok=True)
    calls = [c for p in output.glob("*/research_*/generation/calls.json") for c in read(p)]
    ranking = [c for p in output.glob("*/ranking/calls.json") for c in read(p)]
    paid = [c for c in calls + ranking if c["kind"] == "live"]
    summary = {
        "archive": str(output.relative_to(ROOT)),
        "runs": len(rows),
        "run_states": {
            w: dict(Counter(r["status"] for r in rows if r["workflow"] == w))
            for w in config["workflows"]
        },
        "new_calls": len(paid),
        "new_generation_calls": sum(c["kind"] == "live" for c in calls),
        "new_ranking_calls": len(ranking),
        "call_states": dict(Counter(c["status"] for c in paid)),
        "input_tokens": sum((c["usage"] or {}).get("input_tokens", 0) for c in paid),
        "output_tokens": sum((c["usage"] or {}).get("output_tokens", 0) for c in paid),
        "unknown_usage_calls": sum(c["usage"] is None for c in paid),
        "cost_cny": None,
        "exact_prompt_replays": sum(c["kind"] != "live" for c in calls),
        "human_semantic_labels": None,
        "semantic_success_rate": None,
        "isolation": read(output / "isolation.json"),
        "results": rows,
    }
    write(destination / "results.json", summary)
    write(destination / "frozen-config.json", config)
    sources = {s["id"]: s for s in config["sources"]}
    table = [
        "# 新资料逐题核验入口",
        "",
        "每份包含原始报告、逐条引用原文及页码。程序核对引用身份，不代替语义判断。"
        "正文复制未修改，输出SHA256绑定原件；空标签表示未获用户确认。",
        "",
        "| 题目 | v4 | v7 |",
        "| --- | --- | --- |",
    ]
    binding = []
    for case in config["cases"]:
        links = []
        for workflow in config["workflows"]:
            arm = output / case["id"] / workflow
            row = next(r for r in rows if r["case"] == case["id"] and r["workflow"] == workflow)
            filename = f"{case['id']}-{workflow}.md"
            run = read(arm / "archive/run.json")
            lines = [
                f"# {case['id']} · {workflow}",
                "",
                case["question"],
                "",
                f"运行状态：{row['status']}；人工语义标签：未核验。",
                "",
                f"运行：`{run['id']}`；输出SHA256：`{row.get('output_sha256')}`。",
                "",
            ]
            original = arm / "archive/output.json"
            if original.exists():
                artifact = read(original)
                package = read(arm / "archive/snapshot.json")
                lines += [
                    "## 原始生成正文（未编辑）",
                    "",
                    artifact["rendered_text"],
                    "",
                    "## 逐条引用原文",
                    "",
                    "以下为PDF提取文本，保留原提取换行与断词；不代表重新审核的金标。"
                    "论文版权归原作者/ACL，按[CC BY 4.0](https://aclanthology.org/faq/copyright/)"
                    "提供摘录；来源与原PDF链接见各条。",
                    "",
                ]
                evidence = {
                    e["evidence_id"]: (b, e)
                    for b in package["knowledge"]["claim_bundles"]
                    for e in b["evidence"]
                }
                for number, finding in enumerate(artifact["structured"]["draft"]["findings"], 1):
                    lines += [f"### 条目 {number}", "", finding["assertion"], ""]
                    for eid in finding["evidence_ids"]:
                        bundle, e = evidence[eid]
                        document = bundle["chunks"][0]["document_id"]
                        source = next(
                            s for s in sources.values() if document.endswith(":" + s["id"])
                        )
                        page = next(
                            c["page_start"]
                            for c in bundle["chunks"]
                            if c["chunk_id"] == e["chunk_id"]
                        )
                        lines += [
                            f"[{source['title']}]({source['pdf_url']}#page={page})，"
                            f"PDF第{page}页；`{eid}`。",
                            "",
                            "```text",
                            e["quote"],
                            "```",
                            "",
                        ]
            else:
                lines += [
                    "未形成正式产物。失败证据保留在原运行中。",
                    "",
                    "失败原因：" + str(run.get("error_message")),
                    "",
                ]
            (destination / filename).write_text("\n".join(lines) + "\n", encoding="utf-8")
            links.append(f"[{row['status']}]({filename})")
            binding.append(
                {
                    "case": case["id"],
                    "workflow": workflow,
                    "output_sha256": row.get("output_sha256"),
                    "human_review": None,
                }
            )
        table.append(f"| {case['id']}：{case['question']} | {' | '.join(links)} |")
    (destination / "cases.md").write_text("\n".join(table) + "\n", encoding="utf-8")
    labels = destination / "human-review.json"
    if not labels.exists():
        write(labels, binding)
    return {"reports": len(rows), "output": str(destination)}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path)
    print(export(parser.parse_args().archive))
