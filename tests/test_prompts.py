from app.prompts import build_agent_system


def test_prompt_lists_releases_and_comparison_caveats(catalog):
    p = build_agent_system(catalog, max_rows=200)
    assert '"2019": ACS 5-year estimates covering 2015-2019' in p
    assert '"2020": ACS 5-year estimates covering 2016-2020' in p
    assert "use 2020 (the most recent)" in p
    assert "share four of their five" in p and "inflation-adjusted dollars" in p
    assert "linear interpolation within a bracket distribution" in p and "Do NOT average block-group medians" in p
    assert "never join block groups across years" in p
    assert '"2020_METADATA_CBG_FIPS_CODES"' in p and "Never call `resolve_geography` once per row" in p
    assert "{" not in p.replace("{}", "")  # no unfilled template placeholders


def test_prompt_is_stable_for_caching(catalog):
    assert build_agent_system(catalog, max_rows=200) == build_agent_system(catalog, max_rows=200)
