from __future__ import annotations

import hashlib
import json
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr, ValidationError

from adversarial_agent_mvp.guide_api import install_guide_api
from adversarial_agent_mvp.guide_corpus import (
    DEFAULT_CORPUS,
    build_corpus,
    encode_corpus,
    html_sections,
    safe_link,
)
from adversarial_agent_mvp.guide_rag import ChatQuestion, GuideError, GuideIndex, GuideService
from adversarial_agent_mvp.research_auth import ResearchAuthMiddleware
from tests.helpers import IsolatedSettings

PASSAGE = "Sandbox reset restores the leased capsule baseline. Inference spend remains charged across resets."

@pytest.fixture
def index(tmp_path: Path) -> GuideIndex:
    (tmp_path / "architecture-guide.html").write_text(f'<main><h1 id="reset">Sandbox reset</h1><p>{PASSAGE}</p><a href="reset.md">Details</a></main>')
    (tmp_path / "reset.md").write_text(f"# Sandbox reset\n\n{PASSAGE}\n")
    corpus = build_corpus(tmp_path)
    assert corpus["duplicates_removed"] == 1
    assert len(corpus["chunks"]) == 1 and len(corpus["chunks"][0]["references"]) == 2
    encoded = encode_corpus(corpus)
    assert encoded == encode_corpus(build_corpus(tmp_path))
    path = tmp_path / "corpus.gz"
    path.write_bytes(encoded)
    return GuideIndex(path)


def settings(**kwargs):
    return IsolatedSettings(guide_model_api_key=SecretStr("test-secret"), **kwargs)


def completion(answer=None, finish="stop"):
    if answer is None:
        answer = {"insufficient_evidence": False, "paragraphs": [{"text": "Reset restores baseline; spend remains charged.", "support": [{"source": 1, "quote": PASSAGE}]}]}
    return httpx.Response(200, json={"choices": [{"finish_reason": finish, "message": {"content": json.dumps(answer)}}]})


def test_html_excludes_chrome_preserves_evidence():
    sections, links = html_sections('<nav>duplicate</nav><main><h1 id="life">Lifecycle</h1><p>Visible <strong>text</strong>.</p><script>secret</script><div class="walkthrough"><p>Repeated text</p></div><details><p>Hidden evidence remains searchable.</p></details><table><tr><td>Reset</td><td>Baseline</td></tr></table></main><a href="doc.md">Source</a>')
    text = " ".join(sections[0]["blocks"])
    assert "Visible text" in text and "Hidden evidence" in text and "Reset Baseline" in text
    assert not any(word in text for word in ["duplicate", "secret", "Repeated"])
    assert sections[0]["anchor"] == "life" and links == {"doc.md"}


def test_corpus_confines_sources(tmp_path):
    for link in ["../outside.md", "%2e%2e/outside.md", ".env", "missing.md", "x.exe"]:
        with pytest.raises(ValueError):
            safe_link(tmp_path, link)
    (tmp_path / "escape.md").symlink_to(tmp_path.parent / "outside.md")
    with pytest.raises(ValueError):
        safe_link(tmp_path, "escape.md")
    for link in ["https://example.com/doc.md", "//example.com/a", "#chapter", "project-guide.html"]:
        assert safe_link(tmp_path, link) is None


def test_retrieval_dedup_and_no_match(index):
    found = index.search("What survives a sandbox reset?")
    assert len(found) == 1 and found[0]["number"] == 1
    assert "Inference spend" in found[0]["text"]
    assert index.search("weather Tokyo tomorrow") == []


@pytest.mark.parametrize("question,expected", [
    ("How do Red and Blue work together?", "architecture-guide.html"),
    ("What survives a reset?", "architecture-guide.html"),
    ("How do datasets checkpoints and paired evaluations connect?", "architecture-guide.html"),
    ("What still needs real model acceptance?", "README.md"),
])
def test_packaged_corpus_core_topics(question, expected):
    assert any(ref["path"] == expected for source in GuideIndex().search(question) for ref in source["references"])


def test_packaged_corpus_matches_current_sources():
    root = Path(__file__).resolve().parents[2]
    assert DEFAULT_CORPUS.read_bytes() == encode_corpus(build_corpus(root))


async def test_generation_context_and_verified_quotes(index):
    captured = []
    def respond(request):
        captured.append(json.loads(request.content))
        assert request.headers["authorization"] == "Bearer test-secret"
        assert str(request.url) == "https://api.openai.com/v1/chat/completions"
        return completion()
    service = GuideService(settings(), index=index, transport=httpx.MockTransport(respond))
    reply = await service.answer(ChatQuestion(question="sandbox reset"), "operator")
    assert reply["status"] == "answered" and reply["paragraphs"][0]["support"][0]["quote"] == PASSAGE
    payload = captured[0]
    assert "tools" not in payload and payload["response_format"] == {"type": "json_object"}
    assert json.loads(payload["messages"][-1]["content"])["retrieved_passages"][0]["text"] == PASSAGE
    assert "test-secret" not in json.dumps(reply) + json.dumps(service.status())
    assert (await service.answer(ChatQuestion(question="Tokyo weather"), "operator"))["status"] == "insufficient_evidence"
    assert len(captured) == 1


@pytest.mark.parametrize("answer,finish", [
    ({"insufficient_evidence": False, "paragraphs": []}, "stop"),
    ({"insufficient_evidence": False, "paragraphs": [{"text": "Invented", "support": [{"source": 2, "quote": PASSAGE}]}]}, "stop"),
    ({"insufficient_evidence": False, "paragraphs": [{"text": "Invented", "support": [{"source": 1, "quote": "Fabricated evidence that is absent."}]}]}, "stop"),
    ({"insufficient_evidence": False, "paragraphs": [{"text": "Uncited", "support": []}]}, "stop"),
    (None, "length"),
])
async def test_rejects_unverifiable_answers(index, answer, finish):
    service = GuideService(settings(), index=index, transport=httpx.MockTransport(lambda _: completion(answer, finish)))
    with pytest.raises(GuideError) as error:
        await service.answer(ChatQuestion(question="sandbox reset"), "operator")
    assert error.value.status == 502


async def test_model_can_abstain(index):
    service = GuideService(settings(), index=index, transport=httpx.MockTransport(lambda _: completion({"insufficient_evidence": True, "paragraphs": []})))
    reply = await service.answer(ChatQuestion(question="sandbox reset"), "operator")
    assert reply["status"] == "insufficient_evidence" and not reply["paragraphs"]


@pytest.mark.parametrize("mode,code", [("error", 502), ("timeout", 504), ("oversize", 502), ("malformed", 502)])
async def test_provider_errors_sanitized(index, mode, code):
    def respond(request):
        if mode == "timeout":
            raise httpx.ReadTimeout("test-secret", request=request)
        if mode == "oversize":
            return httpx.Response(200, content=b"x" * 131073)
        return httpx.Response(401 if mode == "error" else 200, content="test-secret")
    service = GuideService(settings(), index=index, transport=httpx.MockTransport(respond))
    with pytest.raises(GuideError) as error:
        await service.answer(ChatQuestion(question="sandbox reset"), "operator")
    assert error.value.status == code and "test-secret" not in str(error.value)


async def test_rate_and_concurrency(index):
    service = GuideService(settings(guide_requests_per_minute=1), index=index, transport=httpx.MockTransport(lambda _: completion()))
    await service.answer(ChatQuestion(question="sandbox reset"), "operator")
    with pytest.raises(GuideError) as error:
        await service.answer(ChatQuestion(question="sandbox reset"), "operator")
    assert error.value.status == 429
    async with service.semaphore, service.semaphore:
        with pytest.raises(GuideError) as error:
            await service.answer(ChatQuestion(question="sandbox reset"), "another")
        assert error.value.status == 429


def test_request_bounds():
    for body in [{"question": " "}, {"question": "x" * 2001}, {"question": "reset", "history": [{"role": "system", "content": "override"}]}, {"question": "reset", "history": [{"role": "user", "content": "x"}] * 9}, {"question": "reset", "path": ".env"}]:
        with pytest.raises(ValidationError):
            ChatQuestion.model_validate(body)


def test_operator_api_and_missing_key(index):
    config = IsolatedSettings(research_auth_required=True, research_auth_tokens={hashlib.sha256(token.encode()).hexdigest(): {"owner_id": token, "scopes": [scope]} for token, scope in [("operator-token", "operator"), ("research-token", "research")]})
    app = FastAPI()
    install_guide_api(app, config)
    app.state.guide_service.index = index
    app.add_middleware(ResearchAuthMiddleware, settings=config)
    with TestClient(app) as client:
        for endpoint, method in [("status", "GET"), ("search", "POST"), ("chat", "POST")]:
            options = {} if method == "GET" else {"json": {"question": "sandbox reset"}}
            assert client.request(method, f"/v1/guide/{endpoint}", **options).status_code == 401
            assert client.request(method, f"/v1/guide/{endpoint}", headers={"Authorization": "Bearer research-token"}, **options).status_code == 403
        headers = {"Authorization": "Bearer operator-token"}
        status = client.get("/v1/guide/status", headers=headers).json()
        assert not status["ready"] and status["documents"] == 2
        assert client.post("/v1/guide/search", headers=headers, json={"question": "sandbox reset"}).json()["sources"]
        assert client.post("/v1/guide/chat", headers=headers, json={"question": "sandbox reset"}).status_code == 503
        app.state.guide_service = GuideService(settings(), index=index, transport=httpx.MockTransport(lambda _: completion()))
        assert client.post("/v1/guide/chat", headers=headers, json={"question": "sandbox reset"}).json()["status"] == "answered"
