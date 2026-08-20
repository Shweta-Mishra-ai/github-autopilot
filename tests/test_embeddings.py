"""
tests/test_embeddings.py

app/intelligence/embeddings.py sat at 36% coverage: sentence-transformers is
deliberately not in requirements.txt, so the "deps available" half of every
function never ran in CI and its bugs were invisible.

These tests fake the dependency so both halves are exercised, and pin the
encoder-reuse contract: both public functions used to construct
SentenceTransformer(...) per call, re-reading ~90MB of weights for every file
embedded.
"""

from unittest.mock import MagicMock, patch

import pytest

import app.intelligence.embeddings as emb


@pytest.fixture(autouse=True)
def _reset():
    emb.reset_model_cache()
    original = emb._DEPS_AVAILABLE
    yield
    emb._DEPS_AVAILABLE = original
    emb.reset_model_cache()


@pytest.fixture
def model():
    """Pretend sentence-transformers is installed, with a stub encoder."""
    m = MagicMock()
    m.encode.return_value.tolist.return_value = [0.1, 0.2, 0.3]
    emb._DEPS_AVAILABLE = True
    with patch.object(emb, "_get_model", return_value=m):
        yield m


class TestDependencyGate:
    def test_missing_deps_disables_embed_without_raising(self):
        emb._DEPS_AVAILABLE = False
        assert emb.embed_file("o/r", "a.py", "code") is False

    def test_missing_deps_disables_search_without_raising(self):
        emb._DEPS_AVAILABLE = False
        assert emb.search_similar("o/r", "query") == []

    def test_dep_check_is_memoised(self):
        emb._DEPS_AVAILABLE = None
        first = emb._check_deps()
        assert emb._DEPS_AVAILABLE is first
        assert emb._check_deps() is first

    def test_absent_package_is_reported_as_unavailable(self):
        emb._DEPS_AVAILABLE = None
        real_import = __builtins__["__import__"] if isinstance(__builtins__, dict) else __import__

        def _no_sentence_transformers(name, *a, **kw):
            if name == "sentence_transformers":
                raise ImportError("not installed")
            return real_import(name, *a, **kw)

        with patch("builtins.__import__", side_effect=_no_sentence_transformers):
            assert emb._check_deps() is False


class TestEmbedFile:
    def test_returns_true_on_success(self, model):
        assert emb.embed_file("o/r", "a.py", "def f(): pass") is True

    def test_content_is_truncated_before_encoding(self, model):
        emb.embed_file("o/r", "a.py", "x" * 9000)
        assert len(model.encode.call_args[0][0]) == 2000

    def test_encoder_failure_degrades_to_false(self, model):
        model.encode.side_effect = RuntimeError("cuda oom")
        assert emb.embed_file("o/r", "a.py", "code") is False


class TestSearchSimilar:
    def test_returns_a_list(self, model):
        assert emb.search_similar("o/r", "query") == []

    def test_encoder_failure_degrades_to_empty_list(self, model):
        model.encode.side_effect = RuntimeError("boom")
        assert emb.search_similar("o/r", "query") == []


class TestModelReuse:
    def test_model_is_loaded_once_across_many_calls(self):
        """Regression: the encoder was rebuilt on every call, so a push
        touching 30 files performed 30 full model loads."""
        emb._DEPS_AVAILABLE = True
        loads = []

        class _FakeST:
            def __init__(self, name):
                loads.append(name)

            def encode(self, text):
                return MagicMock(tolist=lambda: [0.0])

        fake_module = MagicMock()
        fake_module.SentenceTransformer = _FakeST

        with patch.dict("sys.modules", {"sentence_transformers": fake_module}):
            for i in range(10):
                emb.embed_file("o/r", f"f{i}.py", "code")
            emb.search_similar("o/r", "query")

        assert len(loads) == 1, f"model loaded {len(loads)} times, expected 1"
        assert loads[0] == emb.MODEL_NAME

    def test_reset_forces_a_reload(self):
        emb._DEPS_AVAILABLE = True
        loads = []

        class _FakeST:
            def __init__(self, name):
                loads.append(name)

            def encode(self, text):
                return MagicMock(tolist=lambda: [0.0])

        fake_module = MagicMock()
        fake_module.SentenceTransformer = _FakeST

        with patch.dict("sys.modules", {"sentence_transformers": fake_module}):
            emb.embed_file("o/r", "a.py", "code")
            emb.reset_model_cache()
            emb.embed_file("o/r", "b.py", "code")

        assert len(loads) == 2
