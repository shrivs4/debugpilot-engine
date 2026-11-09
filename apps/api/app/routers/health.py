from fastapi import APIRouter, Depends

from app.config import get_settings
from app.db import check_database
from app.services.ollama_client import OllamaClient, get_ollama_client


router = APIRouter(tags=["health"])


@router.get("/health")
def health(ollama: OllamaClient = Depends(get_ollama_client)) -> dict:
    settings = get_settings()
    database = check_database()
    ollama_status = ollama.status([settings.embedding_model, settings.generation_model])

    if not database["ok"] or not database["pgvector"]:
        status = "error"
    elif not ollama_status["ok"]:
        status = "degraded"
    else:
        status = "ok"

    return {
        "status": status,
        "database": database,
        "ollama": ollama_status,
        "models": {"embedding": settings.embedding_model, "generation": settings.generation_model},
    }
