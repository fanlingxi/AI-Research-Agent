import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

import { api, type KnowledgeDocument, type SourceVersion } from "../lib/api";
import { DocumentVersions } from "./DocumentVersions";

const old: SourceVersion = {
  source_id: "source-old", document_id: "doc-old", title: "论文", version: "content-sha256:12345678",
  parser_version: "pypdf-v1", ingestion_id: "ing-old", ingestion_ids: ["ing-old"],
  has_published_evidence: true, is_active: true, superseded_by_source_id: null, created_at: "2026-01-01",
};
const latest: SourceVersion = { ...old, source_id: "source-new", document_id: "doc-new", version: "content-sha256:abcdefgh", parser_version: "pypdf-v2", ingestion_id: "ing-new", ingestion_ids: ["ing-new"] };

function show(overrides: Partial<KnowledgeDocument> = {}) {
  const document = { ...old, can_reparse: true, versions: [old], ...overrides };
  return render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}><MemoryRouter><DocumentVersions document={document} /></MemoryRouter></QueryClientProvider>);
}

beforeEach(() => {
  vi.spyOn(api, "activationPreview").mockResolvedValue({ source_id: latest.source_id, replaced_version_count: 1, entity_count: 0, relation_count: 0, entity_samples: [], relation_samples: [] });
});

afterEach(() => vi.restoreAllMocks());

it("requires confirmation and shows no-change without creating an extra action", async () => {
  const reparse = vi.spyOn(api, "reparseDocument").mockResolvedValue({ status: "unchanged", ingestion_id: null });
  const user = userEvent.setup();
  show();
  expect(screen.getByText(/解析版本：pypdf-v1/)).toBeInTheDocument();
  await user.click(screen.getByRole("button", { name: "重新解析" }));
  expect(screen.getByText(/旧快照、旧运行和旧报告不变/)).toBeInTheDocument();
  expect(reparse).not.toHaveBeenCalled();
  await user.click(screen.getByRole("button", { name: "取消" }));
  expect(reparse).not.toHaveBeenCalled();
  await user.click(screen.getByRole("button", { name: "重新解析" }));
  await user.click(screen.getByRole("button", { name: "确认重新解析" }));
  expect(await screen.findByRole("status")).toHaveTextContent("解析内容无变化，未创建任务。");
  expect(reparse).toHaveBeenCalledExactlyOnceWith("doc-old");
});

it("keeps unpublished versions in review and blocks missing local PDFs", async () => {
  show({ can_reparse: false, versions: [{ ...latest, has_published_evidence: false, is_active: false }, old] });
  await userEvent.click(screen.getByText("版本记录（2）"));
  expect(screen.getByRole("button", { name: "重新解析" })).toBeDisabled();
  expect(screen.queryByRole("button", { name: "启用新版本" })).not.toBeInTheDocument();
  expect(screen.getByRole("link", { name: "待审核，前往审核" })).toHaveAttribute("href", "/review?ingestion=ing-new");
});

it("activates only the confirmed version and allows switching back", async () => {
  const activate = vi.spyOn(api, "activateSource").mockResolvedValue({ source_id: latest.source_id, status: "active" });
  const user = userEvent.setup();
  const view = show({ versions: [latest, old] });
  await user.click(screen.getByText("版本记录（2）"));
  await user.click(screen.getByRole("button", { name: "启用新版本" }));
  expect(activate).not.toHaveBeenCalled();
  expect(screen.getByText(/所有资料集合/)).toBeInTheDocument();
  await user.click(screen.getByRole("button", { name: "确认启用" }));
  await waitFor(() => expect(activate).toHaveBeenCalledExactlyOnceWith(latest.source_id));
  expect(await screen.findByRole("status")).toHaveTextContent("版本已启用");
  view.unmount();
  show({ versions: [latest, { ...old, is_active: false, superseded_by_source_id: latest.source_id }] });
  await user.click(screen.getByText("版本记录（2）"));
  await user.click(screen.getByRole("button", { name: "切回此版本" }));
  await user.click(screen.getByRole("button", { name: "确认启用" }));
  await waitFor(() => expect(activate).toHaveBeenLastCalledWith(old.source_id));
});

it("keeps the confirmation open when publication precondition fails", async () => {
  vi.spyOn(api, "activateSource").mockRejectedValue(new Error("该版本尚无已审核发布的知识证据"));
  show({ versions: [latest, old] });
  await userEvent.click(screen.getByText("版本记录（2）"));
  await userEvent.click(screen.getByRole("button", { name: "启用新版本" }));
  await userEvent.click(screen.getByRole("button", { name: "确认启用" }));
  expect(await screen.findByText("该版本尚无已审核发布的知识证据")).toBeInTheDocument();
  expect(screen.getByRole("dialog")).toBeInTheDocument();
});

it("previews knowledge loss with examples before activation", async () => {
  vi.mocked(api.activationPreview).mockResolvedValue({ source_id: latest.source_id, replaced_version_count: 1, entity_count: 2, relation_count: 1, entity_samples: ["Old concept"], relation_samples: ["A → B"] });
  show({ versions: [latest, old] });
  await userEvent.click(screen.getByText("版本记录（2）"));
  await userEvent.click(screen.getByRole("button", { name: "启用新版本" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("启用后将有 2 个实体、1 条关系暂不显示");
  expect(screen.getByText(/实体样例：Old concept/)).toBeInTheDocument();
  expect(screen.getByText(/关系样例：A → B/)).toBeInTheDocument();
  expect(api.activationPreview).toHaveBeenCalledWith(latest.source_id);
});

it("shows when no reviewed knowledge will disappear", async () => {
  show({ versions: [latest, old] });
  await userEvent.click(screen.getByText("版本记录（2）"));
  await userEvent.click(screen.getByRole("button", { name: "启用新版本" }));
  expect(await screen.findByText("不会有已审核知识消失")).toBeInTheDocument();
});

it("allows activation if preview fails", async () => {
  vi.mocked(api.activationPreview).mockRejectedValue(new Error("offline"));
  const activate = vi.spyOn(api, "activateSource").mockResolvedValue({ source_id: latest.source_id, status: "active" });
  show({ versions: [latest, old] });
  await userEvent.click(screen.getByText("版本记录（2）"));
  await userEvent.click(screen.getByRole("button", { name: "启用新版本" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("影响预览加载失败");
  await userEvent.click(screen.getByRole("button", { name: "确认启用" }));
  await waitFor(() => expect(activate).toHaveBeenCalledWith(latest.source_id));
});
