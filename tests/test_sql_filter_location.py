"""SQL filter for city/country must tolerate hyphen vs space mismatches.

The DB stores locations with spaces ("New York"). The LLM occasionally
emits hyphenated slugs ("new-york"). After fixing normalize_location the
slug shouldn't appear in must.city anymore — but the SQL clause is now
defensive so the filter still matches even if a hyphenated value sneaks
through (e.g. typed by hand into a chip).
"""

from pipeline.spec import MustFilters
from pipeline.sql_filter import build_filter_sql


def test_city_filter_uses_hyphen_tolerant_comparison():
    must = MustFilters(city="new york")
    where, params = build_filter_sql(must)
    assert "REPLACE(city, '-', ' ')" in where
    assert "new york" in params


def test_country_filter_uses_hyphen_tolerant_comparison():
    must = MustFilters(country="united states")
    where, params = build_filter_sql(must)
    assert "country = ANY" in where
    assert "ANY" in where
    assert "United States" in params[1]
    assert "USA" in params[1]
    assert "US" in params[1]


def test_country_filter_matches_db_usa_when_planner_uses_us():
    must = MustFilters(country="us")
    _, params = build_filter_sql(must)
    assert "USA" in params[1]


def test_filter_omitted_when_city_and_country_unset():
    must = MustFilters()
    where, _ = build_filter_sql(must)
    assert "city" not in where
    # status clause is always present so 'country' substring would only appear
    # if a country filter was actually added.
    assert "country" not in where
