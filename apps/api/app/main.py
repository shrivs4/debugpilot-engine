import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.config import get_settings
from app.db import ensure_schema
from app.routers import debug_runs, health, repositories
from app.services.ollama_client import OllamaError
from app.services.repository_service import recover_interrupted_work


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


@asynccontextmanager
async def lifespan(_: FastAPI):
    settings = get_settings()
    ensure_schema()
    recover_interrupted_work()
    settings.repositories_dir.mkdir(parents=True, exist_ok=True)
    yield


app = FastAPI(title="DebugPilot API", version="0.1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=get_settings().cors_origin_list,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(OllamaError)
async def ollama_error_handler(_: Request, error: OllamaError) -> JSONResponse:
    return JSONResponse(status_code=503, content={"detail": str(error)})


app.include_router(health.router)
app.include_router(repositories.router)
app.include_router(debug_runs.router)
