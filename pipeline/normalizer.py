"""Post-validation normalisation pass.

Applies skill and location aliases to the spec so all downstream code
works with canonical forms (e.g. "js" → "javascript", "bengaluru" → "bangalore").
"""

from __future__ import annotations

from pipeline.aliases import normalize_location, normalize_skill, normalize_skill_list
from pipeline.spec import CanonicalSearchSpec


def normalize_spec(spec: CanonicalSearchSpec) -> CanonicalSearchSpec:
    """Mutate spec in-place normalising skills and locations. Returns spec."""
    # must
    spec.must.skills  = normalize_skill_list(spec.must.skills)
    if spec.must.country:
        spec.must.country = normalize_location(spec.must.country)
    if spec.must.city:
        spec.must.city = normalize_location(spec.must.city)

    # should
    spec.should.skills    = normalize_skill_list(spec.should.skills)
    spec.should.locations = [normalize_location(l) for l in spec.should.locations]

    # must_not
    spec.must_not.skills = normalize_skill_list(spec.must_not.skills)

    # lexical_terms: lowercase + strip only (no alias — we want exact tokens)
    spec.lexical_terms = [t.lower().strip() for t in spec.lexical_terms if t.strip()]

    return spec
