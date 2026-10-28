import type { HistoryPage, IngestionStatistics } from "../lib/api";

export function historyPage<T>(items: T[], total = items.length, offset = 0, limit = 25): HistoryPage<T> {
  return { items, total, offset, limit, next_offset: offset + limit < total ? offset + limit : null };
}

export const emptyIngestionStatistics: IngestionStatistics = {
  total: 0, document_count: 0, published_count: 0, pending_count: 0,
  by_ingestion: {}, by_collection: {}, pending_ingestions: [],
};
