"""Word-boundary matching: a role like 'cto' must not match inside 'Victor'."""

from __future__ import annotations

from pipeline.text_match import norm_skill, phrase_in_text


def test_cto_does_not_match_inside_victor():
    assert phrase_in_text("cto", "structured resume victor walker robotics engineer") is False


def test_cto_matches_real_word():
    assert phrase_in_text("cto", "experienced cto and engineering leader") is True
    assert phrase_in_text("cto", "former cto, scaled the team") is True


def test_multiword_phrase_matches_whole():
    assert phrase_in_text("mechanical engineering", "skills: mechanical engineering, automation") is True
    assert phrase_in_text("mechanical engineering", "mechanical aptitude and software engineering") is False


def test_norm_skill_collapses_separators():
    assert norm_skill("Mechanical-Engineering") == "mechanical engineering"
    assert norm_skill("machine_learning") == "machine learning"
    # hyphenated spec skill now equals space-separated candidate skill
    assert norm_skill("mechanical-engineering") == norm_skill("Mechanical Engineering")


def test_empty_phrase_is_false():
    assert phrase_in_text("", "anything") is False
