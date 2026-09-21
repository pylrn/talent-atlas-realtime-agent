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
    if normalized in {"null", "none", "undefined"}:
        return None
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
    should_themes: list[str] = Field(default_factory=list)
    should_roles: list[str] = Field(default_factory=list)
    should_locations: list[str] = Field(default_factory=list)
    excluded_skills: list[str] = Field(default_factory=list)
    keyword_policy: str = "auto"
    filter_fingerprint: str = ""
    branch_fingerprints: dict[str, str] = Field(default_factory=dict)
    plan_fingerprint: str = ""

    @model_validator(mode="after")
    def normalize_and_fingerprint(self) -> SearchPlanRevision:
        self.query = _text(self.query) or ""
        self.city = _location(self.city)
        self.country = _text(self.country)
        self.status = _text(self.status)
        self.must_skills = _skills(self.must_skills)
        self.should_skills = _skills(self.should_skills)
        self.should_themes = _skills(self.should_themes)
        self.should_roles = _skills(self.should_roles)
        self.should_locations = _skills(self.should_locations)
        self.excluded_skills = _skills(self.excluded_skills)

        # The hard eligibility clause is built once from the revision and then
        # inlined as a CTE into *every* branch query, so it is a shared input
        # rather than a property of one branch. Naming it separately is what
        # makes selective cancellation honest: a rewritten query leaves the
        # skill and count branches reusable, while a change to the hard filter
        # invalidates all four, because rows fetched under a different
        # eligibility clause are simply not the same rows.
        self.filter_fingerprint = _fingerprint({
            "status": self.status,
            "country": self.country,
            "city": self.city,
            "min_years_exp": self.min_years_exp,
            "max_years_exp": self.max_years_exp,
            "must_skills": self.must_skills,
            "excluded_skills": self.excluded_skills,
        })
        shared = {"filter": self.filter_fingerprint}

        self.branch_fingerprints = {
            # Dense retrieval embeds the semantic query, restricted to the
            # eligible pool.
            "vector": _fingerprint({"query": self.query, **shared}),
            # Lexical retrieval adds the keyword policy decision, which decides
            # whether the branch runs at all.
            "bm25": _fingerprint({
                "query": self.query,
                "keyword_policy": self.keyword_policy,
                **shared,
            }),
            # The skill branch scores must/should skills over the same pool.
            "skills": _fingerprint({
                "must": self.must_skills,
                "should": self.should_skills,
                "excluded": self.excluded_skills,
                **shared,
            }),
            # The count branch *is* the eligibility clause and nothing else.
            "sql": _fingerprint({"count": True, **shared}),
        }

        # A composite key over every branch plus the soft signals that steer
        # doc-type weighting at fusion time. Two revisions sharing this key are
        # guaranteed to produce identical retrieval work, so the session may
        # serve a stored result without touching the database at all. The soft
        # signals are covered here rather than in a branch because they shape
        # the fused answer without being owned by any single branch.
        self.plan_fingerprint = _fingerprint({
            "branches": self.branch_fingerprints,
            "targets": {
                "themes": self.should_themes,
                "roles": self.should_roles,
                "locations": self.should_locations,
            },
        })
        return self

    @classmethod
    def create(cls, *, query: str, **values: Any) -> SearchPlanRevision:
        return cls(query=query, **values)

    def patch(self, **changes: Any) -> SearchPlanRevision:
        values = self.model_dump(
            exclude={
                "revision_id",
                "parent_revision_id",
                "filter_fingerprint",
                "branch_fingerprints",
                "plan_fingerprint",
            }
        )
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
    return revision.model_dump(
        exclude={
            "revision_id",
            "parent_revision_id",
            "filter_fingerprint",
            "branch_fingerprints",
            "plan_fingerprint",
        }
    )


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
