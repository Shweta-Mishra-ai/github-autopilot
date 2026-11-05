"""
app/ai/model_catalog.available_models and _extract_ids — the code that reads
a provider's model list when a configured model has been retired. Every other
test stubs these out, so the real parsing and failure handling was untested.
"""

from unittest.mock import MagicMock, patch

import pytest

from app.ai import model_catalog as MC

# conftest stubs available_models for every test (so nothing reaches a
# provider). Captured at import, before that fixture runs, to test the real one.
available_models = MC.available_models


@pytest.fixture(autouse=True)
def _fresh():
    MC.clear_cache()
    yield
    MC.clear_cache()


def _resp(status, body):
    r = MagicMock(status_code=status)
    r.json.return_value = body
    return r


def test_gemini_listing_drops_models_that_cannot_generate():
    payload = {"models": [
        {"name": "models/gemini-flash-latest", "supportedGenerationMethods": ["generateContent"]},
        {"name": "models/text-embedding-004", "supportedGenerationMethods": ["embedContent"]},
        {"name": "models/gemini-pro-latest"},  # no methods listed: kept
    ]}
    assert MC._extract_ids("gemini", payload) == ["gemini-flash-latest", "gemini-pro-latest"]


def test_openai_shaped_listing():
    assert MC._extract_ids("groq", {"data": [{"id": "a"}, {"id": ""}, {}]}) == ["a"]


def test_a_successful_fetch_is_cached(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    get = MagicMock(return_value=_resp(200, {"models": [{"name": "models/gemini-flash-latest"}]}))
    with patch("requests.get", get):
        assert available_models("gemini") == ["gemini-flash-latest"]
        assert available_models("gemini") == ["gemini-flash-latest"]
    assert get.call_count == 1
    assert get.call_args.kwargs["headers"] == {"x-goog-api-key": "k"}, "key in a header, not the URL"


@pytest.mark.parametrize("outcome", [_resp(401, {}), ConnectionError("down")])
def test_a_failed_fetch_is_no_information_not_an_empty_catalogue(monkeypatch, outcome):
    """[] must mean "could not find out"; it is never cached as an answer."""
    monkeypatch.setenv("GROQ_API_KEY", "k")
    kw = {"side_effect": outcome} if isinstance(outcome, Exception) else {"return_value": outcome}
    with patch("requests.get", **kw) as get:
        assert available_models("groq") == []
        assert available_models("groq") == []
    assert get.call_count == 2, "a failure is retried, not cached"


def test_no_key_means_no_request(monkeypatch):
    # (OpenRouter's catalogue is public and is fetched without a key.)
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    with patch("requests.get") as get:
        assert available_models("groq") == []
    get.assert_not_called()


def test_a_key_in_an_exception_is_not_logged(monkeypatch, caplog):
    monkeypatch.setenv("GEMINI_API_KEY", "AIzaSy" + "x" * 33)
    err = ConnectionError("Max retries exceeded with url: /v1beta/models?key=AIzaSy" + "x" * 33)
    with patch("requests.get", side_effect=err):
        available_models("gemini")
    assert "AIzaSy" + "x" * 33 not in caplog.text
