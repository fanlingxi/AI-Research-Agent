import type { QueryClient } from "@tanstack/react-query";

/** Refresh all views affected by a human or AI knowledge review. */
export function invalidateKnowledgeReview(client: QueryClient, ingestionId?: string) {
  for (const key of ["knowledge-candidates", "knowledge-duplicates", "knowledge-map", "knowledge-map-node", "knowledge-ingestions", "knowledge-collection", "knowledge-collections", "knowledge-health"]) {
    const queryKey = ingestionId && ["knowledge-candidates", "knowledge-duplicates"].includes(key) ? [key, ingestionId] : [key];
    void client.invalidateQueries({ queryKey });
  }
}
