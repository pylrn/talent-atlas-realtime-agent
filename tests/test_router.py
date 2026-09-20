"""Tests for the heuristic route detector."""

from pipeline.router import detect_route


def test_jd_overrides_everything():
    assert detect_route(query="hi", jd="long JD here") == "jd_mode"


def test_name_pattern_is_lookup():
    assert detect_route(query="Malcolm Price") == "lookup"
    assert detect_route(query="Priya Sharma")  == "lookup"


def test_long_query_becomes_jd_mode():
    long_text = " ".join(["word"] * 60)
    assert detect_route(query=long_text) == "jd_mode"


def test_empty_query_is_filters_only():
    assert detect_route(query="", jd=None) == "filters_only"
    assert detect_route(query="   ", jd=None) == "filters_only"


def test_pure_filter_words_are_filters_only():
    # Words that all live in _FILTER_ONLY_WORDS
    assert detect_route(query="active candidates") == "filters_only"
    assert detect_route(query="hired or active") == "filters_only"


def test_short_descriptive_is_query_mode():
    assert detect_route(query="senior python developer") == "query_mode"
