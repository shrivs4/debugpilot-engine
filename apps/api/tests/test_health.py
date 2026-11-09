def test_health_reports_database_pgvector_and_ollama(client):
    response = client.get("/health")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["database"]["ok"] is True
    assert body["database"]["pgvector"]
    assert body["ollama"]["ok"] is True
    assert body["models"] == {"embedding": "nomic-embed-text", "generation": "qwen2.5-coder:7b"}


def test_health_is_degraded_when_ollama_is_down(client, fake_ollama):
    fake_ollama.available = False

    body = client.get("/health").json()

    assert body["status"] == "degraded"
    assert body["database"]["ok"] is True
    assert body["ollama"]["ok"] is False


def test_real_client_reports_unreachable_ollama_clearly():
    from app.services.ollama_client import OllamaClient, OllamaUnavailableError

    client = OllamaClient("http://127.0.0.1:9", timeout_seconds=2)
    status = client.status(["nomic-embed-text"])

    assert status["ok"] is False
    assert "Ollama is not reachable at http://127.0.0.1:9" in status["error"]
    try:
        client.embed(["hello"], "nomic-embed-text")
    except OllamaUnavailableError as error:
        assert "OLLAMA_BASE_URL" in str(error)
    else:
        raise AssertionError("expected OllamaUnavailableError")
