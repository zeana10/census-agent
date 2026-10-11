"""The schema catalog: what the agent is allowed to know about the dataset.

The Open Census share has ~7,500 columns spread over ~25 physical tables whose
names (B01001e1, ...) are meaningless without the metadata tables. Rather than
hard-coding a handful of columns into the prompt, we:

  * load the metadata tables once (field descriptions, FIPS codes) and build an
    in-memory catalog, cached to disk so cold starts don't hit Snowflake;
  * put a compact list of ACS *table titles* (~one line each) in the system prompt;
  * expose `search_fields` / `describe_table` tools so the model can drill into
    the exact columns it needs at question time.

Multiple ACS releases are supported (the share has 2019 = 2015-2019 and
2020 = 2016-2020 five-year estimates). Field ids are stable across releases, so
there is one field catalog; each table records which physical table holds it in
each year, and each field records the years it exists in.

Everything is discovered from INFORMATION_SCHEMA, so adding a year or pointing
at a renamed share is a config change, not a code change.
"""

from __future__ import annotations

import json
import logging
import math
import re
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .warehouse import Warehouse

log = logging.getLogger(__name__)

STATE_NAMES = {
    "AL": "Alabama",
    "AK": "Alaska",
    "AZ": "Arizona",
    "AR": "Arkansas",
    "CA": "California",
    "CO": "Colorado",
    "CT": "Connecticut",
    "DE": "Delaware",
    "DC": "District of Columbia",
    "FL": "Florida",
    "GA": "Georgia",
    "HI": "Hawaii",
    "ID": "Idaho",
    "IL": "Illinois",
    "IN": "Indiana",
    "IA": "Iowa",
    "KS": "Kansas",
    "KY": "Kentucky",
    "LA": "Louisiana",
    "ME": "Maine",
    "MD": "Maryland",
    "MA": "Massachusetts",
    "MI": "Michigan",
    "MN": "Minnesota",
    "MS": "Mississippi",
    "MO": "Missouri",
    "MT": "Montana",
    "NE": "Nebraska",
    "NV": "Nevada",
    "NH": "New Hampshire",
    "NJ": "New Jersey",
    "NM": "New Mexico",
    "NY": "New York",
    "NC": "North Carolina",
    "ND": "North Dakota",
    "OH": "Ohio",
    "OK": "Oklahoma",
    "OR": "Oregon",
    "PA": "Pennsylvania",
    "RI": "Rhode Island",
    "SC": "South Carolina",
    "SD": "South Dakota",
    "TN": "Tennessee",
    "TX": "Texas",
    "UT": "Utah",
    "VT": "Vermont",
    "VA": "Virginia",
    "WA": "Washington",
    "WV": "West Virginia",
    "WI": "Wisconsin",
    "WY": "Wyoming",
    "PR": "Puerto Rico",
    "AS": "American Samoa",
    "GU": "Guam",
    "MP": "Northern Mariana Islands",
    "VI": "U.S. Virgin Islands",
}


@dataclass
class FieldInfo:
    id: str  # physical column name, e.g. "B19013e1"
    table_number: str  # ACS table, e.g. "B19013"
    label: str  # "Median household income in the past 12 months (...)", "Total: Renter occupied"
    kind: str  # "estimate" | "margin_of_error"
    years: list[str] = field(default_factory=list)  # releases whose tables contain this column


@dataclass
class TableInfo:
    number: str
    title: str
    universe: str
    physical_tables: dict[str, str]  # year -> physical table, e.g. {"2019": "2019_CBG_B19", "2020": "2020_CBG_B19"}
    field_ids: list[str] = field(default_factory=list)


@dataclass
class County:
    state_fips: str
    county_fips: str  # 3 digits
    name: str
    state_abbr: str

    @property
    def geoid(self) -> str:
        return self.state_fips + self.county_fips


@dataclass
class State:
    abbr: str
    fips: str
    name: str


@dataclass
class Catalog:
    years: list[str]  # ascending; the last one is the default for questions that don't name a year
    database: str
    schema: str
    physical_tables: dict[str, list[str]]  # table name -> column names
    tables: dict[str, TableInfo]  # ACS table number -> info
    fields: dict[str, FieldInfo]  # column name -> info
    states: list[State]
    counties: list[County]
    geo_table: str | None = None  # table with lat/long/land area per CBG (latest year)

    # ---- derived lookups -------------------------------------------------
    def __post_init__(self):
        self._index = _Bm25([self._doc(f) for f in self.fields.values()], list(self.fields))

    def _doc(self, f: FieldInfo) -> str:
        t = self.tables.get(f.table_number)
        return " ".join(filter(None, [f.label, t.title if t else "", t.universe if t else "", f.id]))

    @property
    def allowed_tables(self) -> set[str]:
        return set(self.physical_tables)

    @property
    def latest_year(self) -> str:
        return self.years[-1]

    def search_fields(self, query: str, *, limit: int = 15, include_moe: bool = False) -> list[FieldInfo]:
        hits = self._index.search(query, limit=limit * 3)
        out = [self.fields[h] for h in hits if include_moe or self.fields[h].kind == "estimate"]
        return out[:limit]

    def table_overview(self) -> str:
        """One line per ACS table, for the system prompt."""
        lines = []
        for t in sorted(self.tables.values(), key=lambda t: t.number):
            n_est = sum(1 for f in t.field_ids if self.fields[f].kind == "estimate")
            # Physical table names differ only by year prefix ("2019_CBG_B19" / "2020_CBG_B19"),
            # so show the pattern once instead of repeating the line per year.
            group = next(iter(t.physical_tables.values())).split("_", 1)[1]
            years = "" if set(t.physical_tables) == set(self.years) else f" | only {', '.join(sorted(t.physical_tables))}"
            lines.append(f'{t.number} | {t.title} | universe: {t.universe} | {n_est} fields | in "<year>_{group}"{years}')
        return "\n".join(lines)

    # ---- persistence -----------------------------------------------------
    def to_json(self) -> str:
        return json.dumps(
            {
                "years": self.years,
                "database": self.database,
                "schema": self.schema,
                "physical_tables": self.physical_tables,
                "tables": {k: asdict(v) for k, v in self.tables.items()},
                "fields": {k: asdict(v) for k, v in self.fields.items()},
                "states": [asdict(s) for s in self.states],
                "counties": [asdict(c) for c in self.counties],
                "geo_table": self.geo_table,
            }
        )

    @classmethod
    def from_json(cls, raw: str) -> Catalog:
        d = json.loads(raw)
        return cls(
            years=d["years"],
            database=d["database"],
            schema=d["schema"],
            physical_tables=d["physical_tables"],
            tables={k: TableInfo(**v) for k, v in d["tables"].items()},
            fields={k: FieldInfo(**v) for k, v in d["fields"].items()},
            states=[State(**s) for s in d["states"]],
            counties=[County(**c) for c in d["counties"]],
            geo_table=d.get("geo_table"),
        )


# ---------------------------------------------------------------------------
# Building the catalog from Snowflake
# ---------------------------------------------------------------------------

_FIELD_ID_RE = re.compile(r"^([A-Z]\d{5}[A-Z]{0,2})([em])(\d+)$")


def build_catalog(wh: Warehouse, *, database: str, schema: str, years: list[str]) -> Catalog:
    years = sorted(years)
    cols = wh.query_sync(
        f"""
        SELECT table_name, column_name
        FROM "{database}".information_schema.columns
        WHERE table_schema = '{schema}'
        ORDER BY table_name, ordinal_position
        """,
        max_rows=100_000,
    )
    physical: dict[str, list[str]] = defaultdict(list)
    for tname, cname in cols.rows:
        physical[tname].append(cname)
    if not physical:
        raise RuntimeError(f"No tables visible in {database}.{schema} - check the role's grants.")

    fields: dict[str, FieldInfo] = {}
    tables: dict[str, TableInfo] = {}
    states: dict[str, State] = {}
    counties: dict[str, County] = {}
    allowed: list[str] = []
    geo_table: str | None = None

    # Oldest first, so the latest release's titles and labels win (e.g. "2020 inflation-adjusted dollars").
    for year in years:
        year_tables = {t: c for t, c in physical.items() if t.startswith(f"{year}_") and "REDISTRICTING" not in t}
        if not year_tables:
            raise RuntimeError(f"No tables for census year {year} in {database}.{schema}.")
        # ACS block-group data tables, e.g. 2019_CBG_B01. The 2020 decennial redistricting tables use a
        # different metadata format and are deliberately not loaded yet (see REFLECTION.md).
        data_tables = {t: c for t, c in year_tables.items() if t.startswith(f"{year}_CBG_")}
        desc_table = _find(year_tables, f"{year}_METADATA_CBG_FIELD_DESCRIPTIONS", "FIELD_DESCRIPTIONS")
        fips_table = _find(year_tables, f"{year}_METADATA_CBG_FIPS_CODES", "FIPS_CODES")
        geo_table = _find_optional(year_tables, f"{year}_METADATA_CBG_GEOGRAPHIC_DATA", "GEOGRAPHIC_DATA") or geo_table
        log.info("catalog %s: %d data tables, desc=%s fips=%s", year, len(data_tables), desc_table, fips_table)
        allowed += [*data_tables, desc_table, fips_table]

        column_to_table = {c: t for t, cs in data_tables.items() for c in cs if _FIELD_ID_RE.match(c)}
        for row in _read_field_descriptions(wh, desc_table, year_tables[desc_table]):
            fid, title, universe, *levels = row
            if not fid or fid not in column_to_table:
                continue  # described but not present in this release; never let the model pick it
            m = _FIELD_ID_RE.match(fid)
            if not m:
                continue
            number, kind_ch, _ = m.groups()
            label = _field_label(levels, title=str(title or ""), universe=str(universe or "")) or fid
            f = fields.get(fid)
            if f is None:
                f = fields[fid] = FieldInfo(
                    id=fid, table_number=number, label=label, kind="estimate" if kind_ch == "e" else "margin_of_error"
                )
            f.label = label
            f.years.append(year)
            t = tables.get(number)
            if t is None:
                t = tables[number] = TableInfo(number=number, title="", universe="", physical_tables={})
            t.title = str(title or "").strip()
            t.universe = str(universe or "").strip()
            t.physical_tables[year] = column_to_table[fid]
            if fid not in t.field_ids:
                t.field_ids.append(fid)

        fips = wh.query_sync(f"SELECT * FROM {_q(fips_table)}", max_rows=100_000)
        fc = [c.upper() for c in fips.columns]
        for frow in fips.rows:
            r = dict(zip(fc, frow, strict=True))
            abbr = str(r.get("STATE", "")).strip().upper()
            sf = str(r.get("STATE_FIPS", "")).zfill(2)
            cf = str(r.get("COUNTY_FIPS", "")).zfill(3)
            if not abbr or not sf.strip("0"):
                continue
            states.setdefault(abbr, State(abbr=abbr, fips=sf, name=STATE_NAMES.get(abbr, abbr)))
            if r.get("COUNTY"):
                # Union across years keyed by GEOID; the latest name wins (counties are occasionally renamed).
                counties[sf + cf] = County(state_fips=sf, county_fips=cf, name=str(r["COUNTY"]).strip(), state_abbr=abbr)

    if geo_table:
        allowed.append(geo_table)
    return Catalog(
        years=years,
        database=database,
        schema=schema,
        physical_tables={t: physical[t] for t in allowed},
        tables=tables,
        fields=fields,
        states=sorted(states.values(), key=lambda s: s.fips),
        counties=list(counties.values()),
        geo_table=geo_table,
    )


def _read_field_descriptions(wh: Warehouse, desc_table: str, dcols: list[str]) -> list[list]:
    """Rows of (field id, table title, universe, *levels), discovering the column layout."""
    id_col = _pick(dcols, "TABLE_ID", "FIELD_ID", "ID")
    title_col = _pick(dcols, "TABLE_TITLE", "TITLE")
    universe_col = _pick_optional(dcols, "TABLE_UNIVERSE", "UNIVERSE")
    level_cols = sorted(
        (c for c in dcols if c.upper().startswith("FIELD_LEVEL")),  # also matches the share's "FIELD_LEVELl_9" typo
        key=lambda c: int(re.sub(r"\D", "", c) or 0),
    )
    select = [_q(id_col), _q(title_col), _q(universe_col) if universe_col else "NULL", *map(_q, level_cols)]
    return wh.query_sync(f"SELECT {', '.join(select)} FROM {_q(desc_table)}", max_rows=100_000).rows


def load_or_build(wh: Warehouse, *, cache_path: str, database: str, schema: str, years: list[str]) -> Catalog:
    p = Path(cache_path)
    if p.exists():
        try:
            cat = Catalog.from_json(p.read_text())
            if (cat.database, cat.schema, cat.years) == (database, schema, sorted(years)):
                log.info("catalog loaded from %s (%d fields)", p, len(cat.fields))
                return cat
        except (KeyError, TypeError, ValueError):
            log.warning("catalog cache %s is from an older format; rebuilding", p)
    cat = build_catalog(wh, database=database, schema=schema, years=years)
    try:
        p.write_text(cat.to_json())
    except OSError:
        log.warning("could not write catalog cache to %s", p)
    log.info("catalog built from Snowflake (%d fields, %d tables, years %s)", len(cat.fields), len(cat.tables), cat.years)
    return cat


def _field_label(levels: list, *, title: str, universe: str) -> str:
    """Human label for a field from the share's FIELD_LEVEL_* columns.

    The share repeats context in the levels: [kind ("Estimate"/"MarginOfError"),
    TABLE TITLE IN CAPS, universe, "Total", breakdown...]. We drop the kind (kept
    separately) and the repeated title/universe, leaving e.g. "Total: Renter occupied".
    The repeats would otherwise dominate keyword search across sibling fields.
    """
    parts = [str(x).strip() for x in levels if x not in (None, "") and str(x).strip()]
    if parts and parts[0].lower() in ("estimate", "marginoferror", "margin of error"):
        parts = parts[1:]
    if parts and parts[0].lower() == title.strip().lower():
        parts = parts[1:]
    if parts and parts[0].lower() == universe.strip().lower():
        parts = parts[1:]
    return ": ".join(parts)


def _find_optional(tables: dict[str, list[str]], exact: str, needle: str) -> str | None:
    """The table named `exact`, else the only table whose name contains `needle`."""
    if exact in tables:
        return exact
    matches = [t for t in tables if needle in t]
    if len(matches) > 1:
        raise RuntimeError(f"Ambiguous metadata tables for {needle!r}: {matches}")
    return matches[0] if matches else None


def _find(tables: dict[str, list[str]], exact: str, needle: str) -> str:
    found = _find_optional(tables, exact, needle)
    if found is None:
        raise RuntimeError(f"Could not find a metadata table containing {needle!r}. Tables seen: {sorted(tables)[:20]}")
    return found


def _pick_optional(cols: list[str], *candidates: str) -> str | None:
    upper = {c.upper(): c for c in cols}
    return next((upper[c] for c in candidates if c in upper), None)


def _pick(cols: list[str], *candidates: str) -> str:
    found = _pick_optional(cols, *candidates)
    if found is None:
        raise RuntimeError(f"None of {candidates} in metadata columns {cols}")
    return found


def _q(ident: str) -> str:
    return '"' + ident.replace('"', '""') + '"'


# ---------------------------------------------------------------------------
# Tiny BM25 - good enough for ~7.5k short documents, no extra dependency.
# ---------------------------------------------------------------------------

_TOKEN_RE = re.compile(r"[a-z0-9]+")
_STOP = {"the", "of", "in", "and", "for", "a", "an", "to", "by", "with", "or", "is", "what", "how", "many"}
_SYNONYMS = {
    "income": ["earnings"],
    "earnings": ["income"],
    "kids": ["children", "under"],
    "children": ["under"],
    "elderly": ["65", "over"],
    "seniors": ["65", "over"],
    "renters": ["renter"],
    "owners": ["owner"],
    "hispanic": ["latino"],
    "latino": ["hispanic"],
    "black": ["african"],
    "commute": ["travel", "work"],
    "poverty": ["poverty"],
    "rent": ["rent", "gross"],
    "college": ["bachelor"],
    "degree": ["bachelor"],
    "homes": ["housing", "units"],
    "houses": ["housing", "units"],
}


def _stem(t: str) -> str:
    # Plural folding only ("veterans" -> "veteran"); a real stemmer over-merges census terms.
    return t[:-1] if len(t) > 3 and t.endswith("s") and not t.endswith(("ss", "us", "is")) else t


def _tokens(text: str) -> list[str]:
    return [_stem(t) for t in _TOKEN_RE.findall(text.lower()) if t not in _STOP]


# Queries and documents are plural-folded, so the synonym table must be too.
_SYNONYMS = {_stem(k): [_stem(v) for v in vs] for k, vs in _SYNONYMS.items()}


class _Bm25:
    def __init__(self, docs: list[str], ids: list[str], k1: float = 1.2, b: float = 0.75):
        self.ids = ids
        self.k1, self.b = k1, b
        self.tf = [Counter(_tokens(d)) for d in docs]
        self.len = [sum(c.values()) for c in self.tf]
        self.avg = (sum(self.len) / len(self.len)) if self.len else 1.0
        df: Counter[str] = Counter()
        for c in self.tf:
            df.update(c.keys())
        n = len(docs)
        self.idf = {t: math.log(1 + (n - f + 0.5) / (f + 0.5)) for t, f in df.items()}
        self.postings: dict[str, list[int]] = defaultdict(list)
        for i, c in enumerate(self.tf):
            for t in c:
                self.postings[t].append(i)

    def search(self, query: str, *, limit: int) -> list[str]:
        # Words the user typed count fully. Synonyms only help when the literal words
        # miss, so they count at a fraction; otherwise "income" -> "earnings" would
        # pull the earnings tables above the household-income ones.
        weights: dict[str, float] = {}
        for t in _tokens(query):
            weights[t] = 1.0
            for syn in _SYNONYMS.get(t, []):
                weights.setdefault(syn, 0.3)
        scores: dict[int, float] = defaultdict(float)
        for t, w in weights.items():
            idf = self.idf.get(t)
            if idf is None:
                continue
            for i in self.postings[t]:
                f = self.tf[i][t]
                scores[i] += w * idf * f * (self.k1 + 1) / (f + self.k1 * (1 - self.b + self.b * self.len[i] / self.avg))
        # Prefer shorter, more general labels (e.g. "Total" rows) on ties.
        ranked = sorted(scores, key=lambda i: (-scores[i], self.len[i]))
        return [self.ids[i] for i in ranked[:limit]]
