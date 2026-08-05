from __future__ import annotations

from app.config.settings import get_settings
from app.schemas.documents import PaperMetadata
from app.tools.search_tools import paper_search


class SearchAgent:
    """Collect candidate papers for a research topic."""

    def search(
        self,
        query: str,
        limit: int | None = None,
        live_search: bool | None = None,
    ) -> list[PaperMetadata]:
        settings = get_settings()
        selected_limit = limit or settings.paper_search_limit
        selected_live_search = settings.search_live_enabled if live_search is None else live_search

        result = paper_search(
            query=query,
            limit=selected_limit,
            live_search=selected_live_search,
        )
        return [
            PaperMetadata.model_validate(paper)
            for paper in result.metadata.get("papers", [])
        ]
