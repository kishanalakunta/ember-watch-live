"""Jurisdiction Configuration Pack loader (PRD §9.3, FR-2, FR-CORE-1).

The core engine only ever sees a validated `Pack` object. Nothing outside
config/jurisdictions/<id>/ knows which country or agency it is serving.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

ROOT = Path(__file__).resolve().parent.parent
PACK_DIR = ROOT / "config" / "jurisdictions"
_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,40}$")


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ProviderRef(Strict):
    id: str
    sources: list[str] = Field(default_factory=list)
    day_range: int = Field(default=3, ge=1, le=5)
    model: str | None = None
    fallback_model: str | None = None
    past_days: int = Field(default=60, ge=7, le=92)
    forecast_days: int = Field(default=3, ge=1, le=7)


class Providers(Strict):
    active_fire: list[ProviderRef] = Field(default_factory=list)
    weather: list[ProviderRef] = Field(default_factory=list)
    incidents: list[ProviderRef] = Field(default_factory=list)


class StaticSource(Strict):
    min_distinct_days: int = Field(default=3, ge=2)
    max_extent_km: float = Field(default=0.8, gt=0)


class Routing(Strict):
    priority_review: float = Field(ge=0, le=1)
    verify: float = Field(ge=0, le=1)
    min_passes_for_priority: int = Field(default=2, ge=1, le=6)


class Detection(Strict):
    cluster_radius_km: float = Field(gt=0, le=20)
    cluster_window_hours: float = Field(gt=0, le=240)
    max_spread_kmh: float = Field(default=2.0, ge=0, le=15)   # lets a moving fire front stay one cluster
    incident_match_km: float = Field(gt=0, le=50)
    exposure_radius_km: float = Field(gt=0, le=200)
    static_source: StaticSource = StaticSource()
    routing: Routing


class RiskClass(Strict):
    name: str
    min: float


class Validation(Strict):
    status: Literal["validated", "provisional", "not_validated"]
    note: str = ""


class Risk(Strict):
    model_id: str
    primary_index: Literal["fwi", "isi", "ffwi", "hdw"]
    classes: list[RiskClass]
    validation: Validation

    @field_validator("classes")
    @classmethod
    def ascending(cls, v: list[RiskClass]) -> list[RiskClass]:
        mins = [c.min for c in v]
        if mins != sorted(mins) or len(v) < 2:
            raise ValueError("risk classes must be >=2 and sorted by min")
        return v


class Geography(Strict):
    boundary: str
    places: str
    grid_spacing_deg: float = Field(gt=0.05, le=2)


class Aviation(Strict):
    regulation_profile: str


class Pack(Strict):
    id: str
    name: str
    country: str
    region: str
    timezone: str
    units: Literal["imperial", "metric"]
    languages: list[str]
    terminology: dict[str, str]
    roles: dict[str, str]
    ecosystems: list[str]
    geography: Geography
    providers: Providers
    detection: Detection
    risk: Risk
    compliance: list[str] = Field(default_factory=list)
    aviation: Aviation

    # filled after load, not from YAML
    boundary_geojson: dict = Field(default_factory=dict, exclude=True)
    places_geojson: dict = Field(default_factory=dict, exclude=True)

    @property
    def boundary_ring(self) -> list[list[float]]:
        return self.boundary_geojson["features"][0]["geometry"]["coordinates"][0]

    @property
    def bbox(self) -> tuple[float, float, float, float]:
        xs = [p[0] for p in self.boundary_ring]
        ys = [p[1] for p in self.boundary_ring]
        return (min(xs), min(ys), max(xs), max(ys))

    def public_summary(self) -> dict:
        """What the UI may show (Screen 1). No secrets live in packs anyway."""
        d = self.model_dump(exclude={"boundary_geojson", "places_geojson"})
        return d


def list_packs() -> list[str]:
    return sorted(p.name for p in PACK_DIR.iterdir() if (p / "pack.yaml").is_file())


def load_pack(pack_id: str) -> Pack:
    if not _ID_RE.match(pack_id):
        raise ValueError(f"invalid jurisdiction id: {pack_id!r}")
    d = (PACK_DIR / pack_id).resolve()
    if PACK_DIR.resolve() not in d.parents or not (d / "pack.yaml").is_file():
        raise FileNotFoundError(f"no jurisdiction pack named {pack_id!r}")
    raw = yaml.safe_load((d / "pack.yaml").read_text(encoding="utf-8"))
    pack = Pack.model_validate(raw)
    if pack.id != pack_id:
        raise ValueError("pack id does not match its folder name")
    geo = pack.geography
    boundary = json.loads(_safe_child(d, geo.boundary).read_text(encoding="utf-8"))
    places = json.loads(_safe_child(d, geo.places).read_text(encoding="utf-8"))
    return pack.model_copy(update={"boundary_geojson": boundary, "places_geojson": places})


def _safe_child(base: Path, name: str) -> Path:
    p = (base / name).resolve()
    if base not in p.parents:
        raise ValueError(f"path escapes pack folder: {name}")
    return p
