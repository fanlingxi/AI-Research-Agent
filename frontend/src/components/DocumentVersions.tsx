import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Link } from "react-router-dom";

import { api, type KnowledgeDocument, type SourceVersion } from "../lib/api";
import { ErrorBlock } from "./AsyncState";
import { Button } from "./ui/button";
import { Dialog, DialogContent, DialogDescription, DialogTitle } from "./ui/dialog";

function versionLabel(version: SourceVersion) {
  return `${version.parser_version || "未记录解析版本"} · ${version.version.replace("content-sha256:", "").slice(0, 8)}`;
}

export function DocumentVersions({ document }: { document: KnowledgeDocument }) {
  const client = useQueryClient();
  const [confirm, setConfirm] = useState<"reparse" | SourceVersion | null>(null);
  const refresh = () => client.invalidateQueries();
  const reparse = useMutation({
    mutationFn: () => api.reparseDocument(document.document_id),
    onSuccess: () => { setConfirm(null); void refresh(); },
  });
  const activate = useMutation({
    mutationFn: (version: SourceVersion) => api.activateSource(version.source_id),
    onSuccess: () => { setConfirm(null); void refresh(); },
  });
  const active = document.versions.filter((item) => item.is_active);
  const busy = reparse.isPending || activate.isPending;
  return (
    <div className="min-w-0 space-y-2 text-xs text-muted-ink">
      <p className="break-words">解析版本：{versionLabel(document)}</p>
      <p className="break-words">当前生效版本：{active.length ? active.map(versionLabel).join("、") : "尚无已发布知识"}</p>
      {document.superseded_by_source_id ? <p>此版本已被取代，可在下方切回。</p> : null}
      <Button size="sm" variant="outline" disabled={!document.can_reparse || busy} onClick={() => { reparse.reset(); activate.reset(); setConfirm("reparse"); }}>重新解析</Button>
      {!document.can_reparse ? <p>登记的本地 PDF 不可用。</p> : null}
      {document.versions.length > 1 ? (
        <details>
          <summary>版本记录（{document.versions.length}）</summary>
          <ul className="mt-2 space-y-2">
            {document.versions.map((version) => (
              <li className="flex flex-wrap items-center gap-2" key={version.source_id}>
                <span className="break-all">{versionLabel(version)}</span>
                {version.has_published_evidence ? (
                  version.is_active && active.length === 1 ? <span>生效中</span> :
                    <Button size="sm" variant="outline" disabled={busy} onClick={() => { activate.reset(); reparse.reset(); setConfirm(version); }}>
                      {version.source_id === document.versions[0]?.source_id ? "启用新版本" : "切回此版本"}
                    </Button>
                ) : <Link className="text-brand hover:underline" to={`/review?ingestion=${encodeURIComponent(version.ingestion_id)}`}>待审核，前往审核</Link>}
              </li>
            ))}
          </ul>
        </details>
      ) : null}
      {reparse.data ? <p role="status">{reparse.data.status === "unchanged" ? "解析内容无变化，未创建任务。" : <>
        {reparse.data.deduplicated ? "已存在本次解析的入库任务。" : "已提交重新解析，抽取的知识需要审核。"}
        <Link className="ml-1 text-brand hover:underline" to={`/review?ingestion=${encodeURIComponent(reparse.data.ingestion_id ?? "")}`}>查看任务与审核</Link>
      </>}</p> : null}
      {activate.isSuccess ? <p role="status">版本已启用，新检索和新快照将使用此版本的证据；旧报告不变。</p> : null}
      {!confirm && (reparse.error || activate.error) instanceof Error ? <ErrorBlock error={(reparse.error || activate.error) as Error} /> : null}
      <Dialog open={confirm !== null} onOpenChange={(open) => { if (!open && !busy) setConfirm(null); }}>
        <DialogContent className="w-[min(520px,calc(100vw-2rem))]">
          <DialogTitle>{confirm === "reparse" ? "重新解析资料" : "启用来源版本"}</DialogTitle>
          <DialogDescription>{confirm === "reparse"
            ? "使用当前解析器读取已登记的本地 PDF。内容发生变化时会生成新版本，放入原资料集合；抽取的实体和关系仍需要审核，可合并到已有实体。审核发布后可启用新版本。旧快照、旧运行和旧报告不变；内容相同则不创建任务。"
            : "此操作会在所有资料集合中启用所选版本，取代同一资料的其他已发布版本。新检索和新快照会排除被取代版本的证据，旧报告不变。之后可在版本记录中切回。"}</DialogDescription>
          {confirm && confirm !== "reparse" ? <p className="mt-3 text-sm">{versionLabel(confirm)}</p> : null}
          {(reparse.error || activate.error) instanceof Error ? <div className="mt-3"><ErrorBlock error={(reparse.error || activate.error) as Error} /></div> : null}
          <div className="mt-5 flex justify-end gap-2">
            <Button variant="outline" disabled={busy} onClick={() => setConfirm(null)}>取消</Button>
            <Button disabled={busy} onClick={() => { if (confirm === "reparse") reparse.mutate(); else if (confirm) activate.mutate(confirm); }}>
              {busy ? "处理中…" : confirm === "reparse" ? "确认重新解析" : "确认启用"}
            </Button>
          </div>
        </DialogContent>
      </Dialog>
    </div>
  );
}
