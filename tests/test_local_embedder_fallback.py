from types import SimpleNamespace

from pipeline.embedder import LocalEmbedder


def test_local_embedder_falls_back_to_cached_model_when_metadata_check_fails(monkeypatch):
    calls = []

    class FakeSentenceTransformer:
        def __init__(self, model_name, **kwargs):
            calls.append((model_name, kwargs))
            if not kwargs.get("local_files_only"):
                raise RuntimeError("network unavailable")

        def get_sentence_embedding_dimension(self):
            return 384

    monkeypatch.setitem(
        __import__("sys").modules,
        "sentence_transformers",
        SimpleNamespace(SentenceTransformer=FakeSentenceTransformer),
    )

    embedder = LocalEmbedder("all-MiniLM-L6-v2")

    assert embedder.dimensions == 384
    assert calls == [
        ("all-MiniLM-L6-v2", {}),
        ("all-MiniLM-L6-v2", {"local_files_only": True}),
    ]
