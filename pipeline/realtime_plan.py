"""Immutable search revisions and branch-level reuse decisions."""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from typing import Any

from pydantic import BaseModel, Field, model_validator

from pipeline.aliases import normalize_location


BRANCHES = ("vector", "bm25", "skills", "sql")


def _text(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = re.sub(r"\s+", " ", value).strip().casefold()
    return normalized or None


def _skills(values: list[str] | None) -> list[str]:
    return sorted({_text(value) for value in (values or []) if _text(value)})


def _location(value: str | None) -> str | None:
    normalized = _text(value)
    return normalize_location(normalized) if normalized else None


def _fingerprint(value: dict[str, Any]) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


class SearchPlanRevision(BaseModel):
    revision_id: str = Field(default_factory=lambda: f"rev_{uuid.uuid4().hex}")
    parent_revision_id: str | None = None
    query: str
    city: str | None = None
    country: str | None = None
    min_years_exp: int | None = None
    max_years_exp: int | None = None
    status: str | None = None
    must_skills: list[str] = Field(default_factory=list)
    should_skills: list[str] = Field(default_factory=list)
    excluded_skills: list[str] = Field(default_factory=list)
    branch_fingerprints: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def normalize_and_fingerprint(self) -> "SearchPlanRevision":
        self.query = _text(self.query) or ""
        self.city = _location(self.city)
        self.country = _text(self.country)
        self.status = _text(self.status)
        self.must_skills = _skills(self.must_skills)
        self.should_skills = _skills(self.should_skills)
        self.excluded_skills = _skills(self.excluded_skills)
        self.branch_fingerprints = {
            "vector": _fingerprint({"query": self.query}),
            "bm25": _fingerprint({"query": self.query}),
            "skills": _fingerprint({
                "must": self.must_skills,
                "should": self.should_skills,
                "excluded": self.excluded_skills,
            }),
            "sql": _fingerprint({
                "city": self.city,
                "country": self.country,
                "min_years_exp": self.min_years_exp,
                "max_years_exp": self.max_years_exp,
                "status": self.status,
            }),
        }
        return self

    @classmethod
    def create(cls, *, query: str, **values: Any) -> "SearchPlanRevision":
        return cls(query=query, **values)

    def patch(self, **changes: Any) -> "SearchPlanRevision":
        values = self.model_dump(exclude={"revision_id", "parent_revision_id", "branch_fingerprints"})
        values.update(changes)
        return SearchPlanRevision(
            **values,
            parent_revision_id=self.revision_id,
        )


class RevisionDiff(BaseModel):
    reused: set[str] = Field(default_factory=set)
    replaced: set[str] = Field(default_factory=set)
    new: set[str] = Field(default_factory=set)
    changed_fields: set[str] = Field(default_factory=set)


def _comparable(revision: SearchPlanRevision) -> dict[str, Any]:
    return revision.model_dump(exclude={"revision_id", "parent_revision_id", "branch_fingerprints"})


def diff_revisions(
    previous: SearchPlanRevision | None,
    current: SearchPlanRevision,
) -> RevisionDiff:
    if previous is None:
        return RevisionDiff(new=set(BRANCHES), changed_fields=set(_comparable(current)))

    reused = {
        branch
        for branch in BRANCHES
        if previous.branch_fingerprints.get(branch) == current.branch_fingerprints.get(branch)
    }
    replaced = set(BRANCHES) - reused
    before = _comparable(previous)
    after = _comparable(current)
    changed_fields = {key for key in before if before.get(key) != after.get(key)}
    return RevisionDiff(reused=reused, replaced=replaced, changed_fields=changed_fields)
