from app.catalog import Catalog, load_or_build

from .fakes import DB, SCHEMA, YEARS


def test_catalog_discovers_tables_for_every_year(catalog):
    expected = {
        f"{y}_{t}"
        for y in ("2019", "2020")
        for t in ("CBG_B01", "CBG_B11", "CBG_B15", "CBG_B19", "METADATA_CBG_FIELD_DESCRIPTIONS", "METADATA_CBG_FIPS_CODES")
    } | {"2020_CBG_B28"}
    assert set(catalog.physical_tables) == expected
    assert catalog.years == ["2019", "2020"] and catalog.latest_year == "2020"
    assert catalog.tables["B19013"].physical_tables == {"2019": "2019_CBG_B19", "2020": "2020_CBG_B19"}
    # kind, repeated title and repeated universe are stripped from the label
    assert catalog.fields["B01001e20"].label == "Total: Male: 65 and 66 years"
    assert catalog.fields["B01003m1"].kind == "margin_of_error"
    assert catalog.fields["B01003e1"].years == ["2019", "2020"]


def test_decoy_and_redistricting_tables_are_excluded(catalog):
    allowed = catalog.allowed_tables
    assert "SECRET_PAYROLL" not in allowed
    assert not any("REDISTRICTING" in t for t in allowed)  # different metadata format; not loaded yet


def test_latest_release_wins_titles_and_labels(catalog):
    assert catalog.fields["B19013e1"].label.startswith("Median household income")
    # With several releases, labels must not claim one release's dollars for all of them.
    assert "release-year inflation-adjusted dollars" in catalog.fields["B19013e1"].label
    assert "2020 inflation-adjusted" not in catalog.fields["B19013e1"].label
    assert "release-year" in catalog.tables["B19013"].title


def test_fields_and_tables_present_in_one_release_only(catalog):
    assert catalog.fields["B28002e2"].years == ["2020"]
    assert catalog.tables["B28002"].physical_tables == {"2020": "2020_CBG_B28"}
    line = next(x for x in catalog.table_overview().splitlines() if x.startswith("B28002"))
    assert "only 2020" in line
    assert "only" not in next(x for x in catalog.table_overview().splitlines() if x.startswith("B01003"))


def test_overview_shows_year_pattern_not_one_line_per_year(catalog):
    lines = catalog.table_overview().splitlines()
    assert len(lines) == len(catalog.tables)
    assert any('in "<year>_CBG_B19"' in x for x in lines)


def test_fields_described_but_missing_from_data_are_dropped(catalog):
    assert "B99999e1" not in catalog.fields
    assert "B99999" not in catalog.tables


def test_search_finds_the_right_field(catalog):
    assert catalog.search_fields("median household income")[0].id == "B19013e1"
    assert catalog.search_fields("bachelor's degree")[0].id == "B15003e22"
    ids = [f.id for f in catalog.search_fields("total population")]
    assert "B01003e1" in ids and "B01003m1" not in ids  # MOE hidden by default
    assert "B01003m1" in [f.id for f in catalog.search_fields("total population", include_moe=True)]
    assert catalog.search_fields("zzzz nonsense") == []


def test_table_overview_lists_every_table(catalog):
    overview = catalog.table_overview()
    for number in catalog.tables:
        assert number in overview


def test_catalog_cache_roundtrip(tmp_path, warehouse, catalog):
    path = tmp_path / "c.json"
    path.write_text(catalog.to_json())
    again = Catalog.from_json(path.read_text())
    assert again.fields.keys() == catalog.fields.keys()
    # load_or_build uses the cache instead of querying when it matches
    warehouse.fail_with = RuntimeError("should not query")
    assert (
        load_or_build(warehouse, cache_path=str(path), database=DB, schema=SCHEMA, years=YEARS).fields.keys()
        == catalog.fields.keys()
    )


def test_cache_is_rebuilt_when_configured_years_change(tmp_path, warehouse, catalog):
    path = tmp_path / "c.json"
    path.write_text(catalog.to_json())
    only_2019 = load_or_build(warehouse, cache_path=str(path), database=DB, schema=SCHEMA, years=["2019"])
    assert only_2019.years == ["2019"] and "2020_CBG_B01" not in only_2019.allowed_tables


def test_cache_from_older_catalog_version_is_rebuilt(tmp_path, warehouse, catalog):
    import json

    path = tmp_path / "c.json"
    d = json.loads(catalog.to_json())
    d["version"] = 1
    d["fields"]["B19013e1"]["label"] = "STALE"
    path.write_text(json.dumps(d))
    fresh = load_or_build(warehouse, cache_path=str(path), database=DB, schema=SCHEMA, years=YEARS)
    assert fresh.fields["B19013e1"].label != "STALE"


def test_single_release_keeps_its_own_dollar_year(warehouse):
    from app.catalog import build_catalog

    only_2019 = build_catalog(warehouse, database=DB, schema=SCHEMA, years=["2019"])
    assert "2019 inflation-adjusted" in only_2019.fields["B19013e1"].label


def test_old_format_cache_is_ignored(tmp_path, warehouse):
    path = tmp_path / "c.json"
    path.write_text('{"year": "2019", "database": "CENSUSDB"}')  # single-year format from before
    assert load_or_build(warehouse, cache_path=str(path), database=DB, schema=SCHEMA, years=YEARS).years == YEARS


def test_counties_are_the_union_across_years(geography):
    assert geography.resolve("Chugach Census Area, AK")[0].geoid == "02063"  # 2020-only county


def test_state_by_name_and_abbreviation(geography):
    for q in ["Texas", "texas", "TX", "Texs"]:
        top = geography.resolve(q)[0]
        assert (top.level, top.geoid) == ("state", "48"), q
    assert geography.resolve("Texas")[0].sql_filter() == "LEFT(\"CENSUS_BLOCK_GROUP\", 2) = '48'"


def test_county_with_state_hint(geography):
    for q in ["Cook County, IL", "cook county illinois", "King County WA", "king, wa"]:
        top = geography.resolve(q)
        assert len(top) == 1 and top[0].level == "county", q
    assert geography.resolve("King County WA")[0].geoid == "53033"


def test_ambiguous_name_returns_all_candidates(geography):
    names = {(m.level, m.geoid) for m in geography.resolve("Washington")}
    assert ("state", "53") in names
    assert ("county", "42125") in names and ("county", "39167") in names


def test_nation_and_unknown_places(geography):
    assert geography.resolve("United States")[0].level == "nation"
    assert geography.resolve("Atlantis") == []
    assert geography.resolve("   ") == []
