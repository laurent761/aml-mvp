"""Operator-scoped documentation RAG, separate from attacker observations."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import JSONResponse

from .guide_rag import ChatQuestion, GuideError, GuideService
from .research_auth import principal
from .settings import Settings


def install_guide_api(app: FastAPI, settings: Settings) -> None:
    app.state.guide_service = GuideService(settings)
    router = APIRouter(prefix="/v1/guide", tags=["Documentation assistant"])

    @app.exception_handler(GuideError)
    async def guide_error(_request: Request, error: GuideError) -> JSONResponse:
        return JSONResponse({"detail": str(error)}, status_code=error.status)

    @router.get("/status")
    def status(request: Request) -> dict[str, Any]:
        principal(request, "operator")
        return app.state.guide_service.status()

    @router.post("/search")
    def search(body: ChatQuestion, request: Request) -> dict[str, Any]:
        principal(request, "operator")
        index = app.state.guide_service.get_index()
        return {"sources": index.search(body.question), "corpus_sha256": index.digest}

    @router.post("/chat")
    async def chat(body: ChatQuestion, request: Request) -> dict[str, Any]:
        identity = principal(request, "operator")
        return await app.state.guide_service.answer(body, identity["owner_id"])

    app.include_router(router)
