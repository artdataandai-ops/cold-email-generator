from __future__ import annotations

from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

from src import sender_profiles, settings
from src.config import OPENAI_API_KEY
from src.db import AppSettings, SenderProfile, get_engine
from src.pipeline import research_pipeline

app = FastAPI(title="Cold-Email Research Assistant", version="0.1.0")
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


@app.on_event("startup")
def _init_db() -> None:
    get_engine()


class ResearchRequest(BaseModel):
    company_url: str = Field(min_length=1)
    company_name: Optional[str] = Field(default=None, max_length=200)
    linkedin_url: Optional[str] = None
    recipient_name: Optional[str] = None
    recipient_role: Optional[str] = None
    sender_profile_id: Optional[str] = None
    # Advanced per-request overrides (None = use saved app default)
    recency_days: Optional[int] = Field(default=None, ge=1, le=365)
    max_discovered_urls: Optional[int] = Field(default=None, ge=1, le=20)
    max_news_items: Optional[int] = Field(default=None, ge=1, le=30)
    search_max_results: Optional[int] = Field(default=None, ge=0, le=15)
    disable_web_search: Optional[bool] = None
    disable_linkedin: Optional[bool] = None
    require_context_for_email: Optional[bool] = None
    linkedin_post_limit: Optional[int] = Field(default=None, ge=1, le=50)
    intent: Optional[str] = Field(default=None, max_length=500)


class AppSettingsIn(BaseModel):
    recency_days: Optional[int] = Field(default=None, ge=1, le=365)
    max_news_items: Optional[int] = Field(default=None, ge=1, le=30)
    max_discovered_urls: Optional[int] = Field(default=None, ge=1, le=20)
    search_max_results: Optional[int] = Field(default=None, ge=0, le=15)
    disable_web_search: Optional[bool] = None
    disable_linkedin: Optional[bool] = None
    require_context_for_email: Optional[bool] = None
    linkedin_post_limit: Optional[int] = Field(default=None, ge=1, le=50)
    default_intent: Optional[str] = Field(default=None, max_length=500)


class ResearchResponse(BaseModel):
    bundle: dict
    email: str
    log_path: Optional[str] = None


class SenderProfileIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=500)
    kb_text: str = Field(default="", max_length=20_000)


@app.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "index.html",
        {"key_configured": bool(OPENAI_API_KEY)},
    )


@app.get("/api/health")
async def health() -> dict:
    return {"ok": True, "openai_key_configured": bool(OPENAI_API_KEY)}


@app.post("/api/research", response_model=ResearchResponse)
async def research(req: ResearchRequest) -> ResearchResponse:
    if not OPENAI_API_KEY:
        raise HTTPException(status_code=400, detail="OPENAI_API_KEY is not set in .env")

    result = await run_in_threadpool(
        research_pipeline,
        company_url=req.company_url.strip(),
        company_name=(req.company_name or "").strip() or None,
        linkedin_url=(req.linkedin_url or "").strip() or None,
        recipient_name=(req.recipient_name or "").strip() or None,
        recipient_role=(req.recipient_role or "").strip() or None,
        sender_profile_id=req.sender_profile_id,
        recency_days=req.recency_days,
        max_discovered_urls=req.max_discovered_urls,
        max_news_items=req.max_news_items,
        search_max_results=req.search_max_results,
        disable_web_search=req.disable_web_search,
        disable_linkedin=req.disable_linkedin,
        require_context_for_email=req.require_context_for_email,
        linkedin_post_limit=req.linkedin_post_limit,
        intent=req.intent,
    )

    return ResearchResponse(
        bundle=result.bundle.model_dump(mode="json"),
        email=result.email,
        log_path=result.log_path,
    )


@app.get("/api/sender-profiles", response_model=list[SenderProfile])
async def list_sender_profiles() -> list[SenderProfile]:
    return sender_profiles.list_profiles()


@app.post("/api/sender-profiles", response_model=SenderProfile)
async def create_sender_profile(req: SenderProfileIn) -> SenderProfile:
    return sender_profiles.create_profile(name=req.name, description=req.description, kb_text=req.kb_text)


@app.put("/api/sender-profiles/{profile_id}", response_model=SenderProfile)
async def update_sender_profile(profile_id: str, req: SenderProfileIn) -> SenderProfile:
    profile = sender_profiles.update_profile(
        profile_id, name=req.name, description=req.description, kb_text=req.kb_text
    )
    if profile is None:
        raise HTTPException(status_code=404, detail="Sender profile not found")
    return profile


@app.delete("/api/sender-profiles/{profile_id}")
async def delete_sender_profile(profile_id: str) -> dict:
    if not sender_profiles.delete_profile(profile_id):
        raise HTTPException(status_code=404, detail="Sender profile not found")
    return {"deleted": profile_id}


@app.get("/api/settings", response_model=AppSettings)
async def get_app_settings() -> AppSettings:
    return settings.get_settings()


@app.put("/api/settings", response_model=AppSettings)
async def update_app_settings(req: AppSettingsIn) -> AppSettings:
    return settings.update_settings(**req.model_dump(exclude_unset=True))


@app.post("/api/settings/reset", response_model=AppSettings)
async def reset_app_settings() -> AppSettings:
    return settings.reset_settings()


@app.exception_handler(Exception)
async def unhandled_exception(_, exc: Exception) -> JSONResponse:
    return JSONResponse(status_code=500, content={"detail": f"{type(exc).__name__}: {exc}"})
