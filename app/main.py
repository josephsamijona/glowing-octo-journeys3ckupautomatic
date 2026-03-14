"""FastAPI application entry point."""
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.api.router import router
from app.core.config import get_settings

settings = get_settings()

app = FastAPI(
    title="S3 Backup Flow",
    description=(
        "Centralised automated database backup service for JHBridge. "
        "Streams MySQL/PostgreSQL dumps to AWS S3 with real-time progress."
    ),
    version="1.0.0",
    docs_url="/api/docs",
    redoc_url="/api/redoc",
    openapi_url="/api/openapi.json",
)

# ---------------------------------------------------------------------------
# CORS
# ---------------------------------------------------------------------------
# Restrict CORS to explicitly configured origins.
# The dashboard is served same-origin, so no broad wildcard is needed.
# Set ALLOWED_ORIGINS="https://yourdomain.com,https://api.yourdomain.com"
_raw_origins = settings.allowed_origins  # comma-separated list or "*"
_origins = (
    ["*"]
    if _raw_origins.strip() == "*"
    else [o.strip() for o in _raw_origins.split(",") if o.strip()]
) or ["*"]

app.add_middleware(
    CORSMiddleware,
    allow_origins=_origins,
    allow_credentials=_raw_origins.strip() != "*",
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "X-API-KEY"],
)

# ---------------------------------------------------------------------------
# Static files & templates
# ---------------------------------------------------------------------------
app.mount("/static", StaticFiles(directory="app/static"), name="static")
templates = Jinja2Templates(directory="app/templates")

# ---------------------------------------------------------------------------
# API routes (prefixed)
# ---------------------------------------------------------------------------
app.include_router(router, prefix="/api/v1")

# ---------------------------------------------------------------------------
# Dashboard routes (server-rendered HTML)
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse, include_in_schema=False)
async def dashboard(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})


@app.get("/login", response_class=HTMLResponse, include_in_schema=False)
async def login(request: Request):
    return templates.TemplateResponse("login.html", {"request": request})
