"""Resolve place names to FIPS codes.

Data is at census-block-group (CBG) level: CENSUS_BLOCK_GROUP is a 12-char GEOID
= state(2) + county(3) + tract(6) + block group(1). States and counties are
therefore filtered with a prefix on that column. We resolve names here, in
code, instead of letting the model guess FIPS codes from memory.

Ambiguity is surfaced, not hidden: "Washington" matches a state and ~30
counties, and the agent is expected to ask which one the user means.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from rapidfuzz import fuzz, process

from .catalog import Catalog, State

_COUNTY_SUFFIX = re.compile(r"\s+(county|parish|borough|census area|city and borough|municipality|municipio)$", re.I)


@dataclass
class GeoMatch:
    level: str  # "state" | "county" | "nation"
    name: str
    geoid: str  # "" for nation
    score: float

    def sql_filter(self) -> str:
        if self.level == "nation":
            return "-- no filter: whole country"
        return f"LEFT(\"CENSUS_BLOCK_GROUP\", {len(self.geoid)}) = '{self.geoid}'"

    def as_dict(self) -> dict:
        return {
            "level": self.level,
            "name": self.name,
            "geoid_prefix": self.geoid,
            "sql_filter": self.sql_filter(),
            "match_score": round(self.score),
        }


_NATION = {"us", "usa", "united states", "the us", "america", "the united states", "nation", "national", "country"}


class Geography:
    def __init__(self, catalog: Catalog):
        self.states = catalog.states
        self.counties = catalog.counties
        self._state_by_abbr = {s.abbr: s for s in self.states}
        self._state_names = {s.name.lower(): s for s in self.states}
        self._county_keys = [f"{_norm_county(c.name)}" for c in self.counties]

    def resolve(self, place: str, *, limit: int = 60) -> list[GeoMatch]:
        raw = place.strip()
        q = raw.lower().strip(" .")
        if not q:
            return []
        if q in _NATION:
            return [GeoMatch("nation", "United States", "", 100)]

        # "Cook County, IL" / "King County WA" / "Austin, Texas"
        county_part, state_hint = q, None
        words = re.split(r"[,\s]+", q)
        for n in (3, 2, 1):  # try the trailing 1-3 words as a state ("new york", "wa")
            if len(words) > n:
                maybe_state = self._match_state(" ".join(words[-n:]), exact=True)
                if maybe_state:
                    county_part, state_hint = " ".join(words[:-n]), maybe_state
                    break

        results: list[GeoMatch] = []
        if state_hint is None:
            st = self._match_state(q)
            if st:
                results.append(GeoMatch("state", st[0].name, st[0].fips, st[1]))

        pool = [(i, c) for i, c in enumerate(self.counties) if state_hint is None or c.state_fips == state_hint[0].fips]
        if pool:
            key = _norm_county(county_part)
            choices = {i: self._county_keys[i] for i, _ in pool}
            for _, score, i in process.extract(key, choices, scorer=fuzz.WRatio, limit=40, score_cutoff=86):
                c = self.counties[i]
                results.append(GeoMatch("county", f"{c.name}, {c.state_abbr}", c.geoid, score))

        # Exact name matches first, then by score; keep the list short for the model.
        results.sort(key=lambda g: (-g.score, g.level != "state", g.name))
        top = results[0].score if results else 0
        return [g for g in results if g.score >= top - 6][:limit]

    def _match_state(self, q: str, *, exact: bool = False) -> tuple[State, float] | None:
        q = q.strip().lower()
        if len(q) == 2 and q.upper() in self._state_by_abbr:
            return self._state_by_abbr[q.upper()], 100.0
        if q in self._state_names:
            return self._state_names[q], 100.0
        if exact:
            return None
        hit = process.extractOne(q, list(self._state_names), scorer=fuzz.ratio, score_cutoff=88)
        if hit:
            return self._state_names[hit[0]], float(hit[1])
        return None


def _norm_county(name: str) -> str:
    return _COUNTY_SUFFIX.sub("", name.strip().lower())
