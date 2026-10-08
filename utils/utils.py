"""
utils.py - Shared anonymization primitives
============================================

Common, format-agnostic building blocks used by anym_x and restore_x files:
a deterministic seed-based name anonymizer, mapping
JSON I/O, deterministic UUID generation, and a deterministic GPS
coordinate transform. Keeping these here ensures that the same input
value always maps to the same anonymized output across all three
input formats (PowerFactory, CGMES, CSV, JSON), so a given asset's name,
ID, and location stay consistent regardless of which file it appears in.

Contents
--------
- SeededNameAnonymizer
    Stateful, deterministic string -> "ANON_<hash>" anonymizer. Reuses
    an existing mapping if a value was already translated, derives new
    tokens via SHA-256(seed + value), and extends the hash length on
    collision to guarantee a 1:1 mapping. Holds three related but
    separate mapping tables: a unified forward/reverse string mapping
    (`forward`/`reverse`) for names and free-text attributes, a
    CIM RDF ID mapping (`cim_forward`) for UUID-like identifiers,
    and a GPS mapping (`gps_mapping`) keyed by an object's original
    identifier.

- save_mapping_json / load_mapping_json
    Serialize/deserialize a SeededNameAnonymizer's full state
    (seed, prefix, hash length, and all three mapping tables) to/from
    a JSON file, so anonymization can later be reversed. `load_mapping_json`
    also transparently migrates older mapping files that used the
    legacy `loc_name_mapping` / `attr_mappings` keys into the current
    unified `anon_mapping` format.

- _generate_seeded_uuid
    Deterministically derives a CIM-style UUID (`_xxxxxxxx-xxxx-...`)
    from an original ID and the seed, for optionally remapping
    rdf:ID-style identifiers.

- _build_geo_transform
    Builds a deterministic GPS coordinate transform function from the
    seed: a rotation + mirror + translation applied in normalized
    [-1, 1] lat/lon space (to avoid distortion from rotating raw
    degree coordinates), producing a strong but reversible-via-mapping
    geographic displacement (e.g. Europe -> Africa/Asia). Used together
    with `_scale_back_to_valid_geo` (defined locally in each format
    module) to keep results within valid lat/lon bounds.

- _obj_unit_from_name
    Deterministic seed+tag+name -> [0, 1) float, used to derive
    per-object jitter (radius/angle) so nearby objects don't all
    shift identically.

- _meters_to_deg_lat / _meters_to_deg_lon
    Small-distance conversion helpers (meters -> degrees) used to
    apply metric-scale GPS jitter on top of the global transform,
    accounting for longitude convergence at higher latitudes.

All anonymization in this module is deterministic given the same
seed and input: re-running anonymization with the same seed always
reproduces the same anonymized output, and is fully reversible given
the resulting mapping JSON.
"""

import hashlib
import json
import math
import re
from pathlib import Path
from typing import Dict, List, Tuple

MAX_ALTERATION = 10


# ----------------------------
# Anonymizer container (UNIFIED STRING MAPPING)
# ----------------------------
class SeededNameAnonymizer:
    """Unified seeded anonymizer for string values and special mappings."""

    def __init__(
        self,
        seed: str,
        prefix: str = "ANON_",
        alteration_factor: float = 5,
        length: int = 10,
    ):
        self.seed = str(seed)
        self.prefix = prefix
        self.length = int(length)
        self.alteration_factor = (
            alteration_factor if alteration_factor <= MAX_ALTERATION else MAX_ALTERATION
        )
        # One unified mapping for all ANON_* string anonymizations:
        # original -> anon
        self.forward: Dict[str, str] = {}
        # anon -> original
        self.reverse: Dict[str, str] = {}

        # cimRdfId mapping stays separate (uuid-like, not ANON_*)
        self.cim_forward: Dict[str, str] = {}  # old -> new

        # gps mapping keyed by ORIGINAL cimRdfId (before change)
        self.gps_mapping: Dict[str, dict] = {}

        # mapping which line was used before to restore original length and impedance values
        self.line_mapping: Dict[str, str] = {}

        self.project_unit_system: int = 0
        self.project_unit: str = ""

        # mapping when the time for case studies are set
        time_adding = int(get_hash_str(seed, "study_casereset"), 16)
        self.time_adding: int = int(
            time_adding % 1000000000  # 1 Billion seconds ~= 30 Years
        )
        if self.time_adding % 2 == 0:
            self.time_adding = -self.time_adding
        self.time_mapping: Dict[str, str] = {}

        self.impedance_mapping: Dict[str, dict] = {}

    def translate_attr(self, attr: str, value: str) -> str:  # type: ignore # pylint:disable=unused-argument
        """
        Anonymize an attribute value via the unified string mapping.

        `attr` is accepted for API compatibility but not used to vary the
        mapping (all attributes share one forward/reverse table).
        """

        # attr is intentionally ignored now (unified mapping)
        old = "" if value is None else str(value)
        return self.translate(old)

    def get_hash(self, text: str, length: int) -> str:
        """Public wrapper around `get_hash string` for deriving a
        deterministic hash of arbitrary text with the self.seed."""
        return get_hash_str(self.seed, text, length).upper()

    def translate(self, name: str) -> str:
        """
        Deterministically anonymize a string, reusing any existing mapping.
        """

        if not name:
            return name
        if name.startswith(self.prefix):
            return name
        if name in self.reverse:
            # already anon token
            return name
        if name in self.forward:
            return self.forward[name]

        token = self.get_hash(name, self.length)
        new_name = f"{self.prefix}{token}"

        l = self.length
        while new_name in self.reverse and self.reverse[new_name] != name:
            l += 2
            token = self.get_hash(name, l)
            new_name = f"{self.prefix}{token}"

        self.forward[name] = new_name
        self.reverse[new_name] = name
        return new_name

    def add_time(self, old_time: int) -> int:
        """
        adds the time adding value to the given time. Both are given in seconds from 01.01.1970.

        Parameters
        ----------
        self: object
            self object
        old_time : int
            The old time to be added with the time_adding
        """

        # just adding both ints together
        new_time = old_time + self.time_adding

        # if the new time is below 0, because of time_adding being negative
        # subtract it instead
        if new_time < 0:
            new_time = old_time - self.time_adding

        # if the new time is higher than the maximum limit subtract old_time from time_adding
        if new_time >= 2**32:  # internal edge value for time is 2**32
            new_time = self.time_adding - old_time
        self.time_mapping[str(old_time)] = str(new_time)
        return new_time


def save_mapping_json(path: Path, anonymizer: SeededNameAnonymizer):
    """Serialize anonymizer state to a JSON mapping file."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    data = {
        "seed": anonymizer.seed,
        "prefix": anonymizer.prefix,
        "length": anonymizer.length,
        "time_mapping": anonymizer.time_mapping,
        "unit_system": anonymizer.project_unit_system,
        "unit": anonymizer.project_unit,
        # unified mapping for all ANON_* strings
        "anon_mapping": anonymizer.forward,
        "line_mapping": anonymizer.line_mapping,
        # keep separate
        "cimRdfId_mapping": anonymizer.cim_forward,
        "gps_mapping": anonymizer.gps_mapping,
    }
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def load_mapping_json(path: Path) -> dict:
    """
    Loads mapping and supports migration from older JSONs that had:
      - loc_name_mapping
      - attr_mappings
    into:
      - anon_mapping
    """
    data = json.loads(Path(path).read_text(encoding="utf-8"))

    if "anon_mapping" not in data:
        merged: Dict[str, str] = {}
        merged.update(data.get("loc_name_mapping", {}) or {})

        attr_maps = data.get("attr_mappings", {}) or {}
        if isinstance(attr_maps, dict):
            for _, mp in attr_maps.items():
                if isinstance(mp, dict):
                    merged.update(mp)

        data["anon_mapping"] = merged

    return data


def get_mappings(
    mapping_path: Path,
) -> Tuple[
    Dict[str, Dict[str, str]],
    Dict[str, str],
    Dict[str, str],
    Dict[str, str],
    Dict[str, str],
    Dict[str, dict],
    str,
]:
    """
    Load a mapping JSON and unpack it into the individual lookup tables.

    Parameters
    ----------
    mapping_path : Path
        Path to the mapping JSON produced by `save_mapping_json`.

    Returns
    -------
    tuple
        `(line_map, anon_rev, time_rev, cim_rev, cim_map, gps_map, prefix)`
        where:
        - `line_map` : Dict[str, Dict[str, str]] - original -> anon line info
        - `anon_rev` : Dict[str, str] - anon string -> original string
        - `time_rev` : Dict[str, str] - new time -> old time
        - `cim_rev` : Dict[str, str] - new CIM ID -> old CIM ID
        - `cim_map` : Dict[str, str] - old CIM ID -> new CIM ID
        - `gps_map` : Dict[str, dict] - original ID -> GPS mapping info
        - `prefix` : str - the anonymization token prefix (e.g. "ANON_")
    """
    data = load_mapping_json(mapping_path)

    line_map: Dict[str, Dict[str, str]] = (
        data.get("line_mapping", {}) or {}
    )  # original -> anon

    anon_map: Dict[str, str] = data.get("anon_mapping", {}) or {}  # original -> anon
    anon_rev: Dict[str, str] = {v: k for k, v in anon_map.items()}  # anon -> original

    time_map: Dict[str, str] = data.get("time_mapping", {}) or {}  # old -> new
    time_rev: Dict[str, str] = {v: k for k, v in time_map.items()}  # anon -> original

    cim_map: Dict[str, str] = data.get("cimRdfId_mapping", {}) or {}  # old -> new
    cim_rev: Dict[str, str] = {v: k for k, v in cim_map.items()}  # new -> old

    gps_map: Dict[str, dict] = data.get("gps_mapping", {}) or {}

    unit_system = data.get("unit_system", 0) or 0
    unit = data.get("unit", "") or ""

    prefix = data.get("prefix", "ANON_") or "ANON_"

    return (
        line_map,
        anon_rev,
        time_rev,
        cim_rev,
        cim_map,
        gps_map,
        prefix,
        unit,
        unit_system,
    )


# ----------------------------
# Deterministic CIM id
# ----------------------------
def generate_seeded_uuid(old_id: str, seed: str) -> str:
    """
    Deterministically derive a CIM-style UUID from an original ID and seed.

    Parameters
    ----------
    old_id : str
        The original identifier to remap (leading "_" is stripped).
    seed : str
        Seed value that makes the derived UUID reproducible.

    Returns
    -------
    str
        A new UUID string of the form "_xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx".
    """
    clean = str(old_id).lstrip("_")
    hex32 = get_hash_str(seed, clean, 32)
    uuid = f"{hex32[:8]}-{hex32[8:12]}-{hex32[12:16]}-{hex32[16:20]}-{hex32[20:32]}"
    return "_" + uuid


def build_geo_transform(seed: str, max_shift_frac: float = 0.45):
    """
    Rotation + translation in normalized coordinate space.

    lat/90 and lon/180 are normalized to [-1, 1]; a seed-based rotation +
    translation is applied there, then mapped back to degrees. This avoids
    invalid coordinates from rotating in raw degree space (where lat/lon
    is not a Euclidean space) while still producing strong anonymization:
    points in Europe typically end up in Africa or Asia.

    max_shift_frac=0.45 corresponds to up to ±40.5° lat / ±81° lon shift
    in addition to the rotation. _scale_back_to_valid_geo catches edge cases.
    """

    angle = 2.0 * math.pi * get_hash_float(seed, "gps_angle|")
    mirror = get_hash_float(seed, "gps_mirror|") > 0.5
    dx = (2.0 * get_hash_float(seed, "gps_dx|") - 1.0) * max_shift_frac
    dy = (2.0 * get_hash_float(seed, "gps_dy|") - 1.0) * max_shift_frac
    c, s = math.cos(angle), math.sin(angle)

    def transform(lat: float, lon: float) -> Tuple[float, float]:
        x = lon / 180.0  # normieren auf [-1, 1]
        y = lat / 90.0
        if mirror:
            x = -x  # Achsenspiegelung für zusätzliche Obfuskation
        xr = c * x - s * y  # Rotation im normierten Raum
        yr = s * x + c * y
        xr += dx  # Verschiebung
        yr += dy
        return yr * 90.0, xr * 180.0  # zurück auf Grad

    return transform


def meters_to_deg_lat(m: float) -> float:
    """
    Convert a distance in meters to degrees of latitude.

    Uses the standard approximation of ~111.32 km per degree of
    latitude, which is effectively constant across the globe.

    Parameters
    ----------
    m : float
        Distance in meters.

    Returns
    -------
    float
        Equivalent distance in degrees of latitude.
    """
    return m / 111_320.0


def meters_to_deg_lon(m: float, lat_deg: float) -> float:
    """
    Convert a distance in meters to degrees of longitude at a given latitude.

    Accounts for the convergence of meridians at higher latitudes by
    scaling the degrees-per-meter conversion with `cos(lat_deg)`,
    clamped to a minimum factor of 0.1 to avoid blowing up near the
    poles.

    Parameters
    ----------
    m : float
        Distance in meters.
    lat_deg : float
        Latitude in degrees at which the conversion is evaluated.

    Returns
    -------
    float
        Equivalent distance in degrees of longitude at `lat_deg`.
    """
    coslat = abs(math.cos(math.radians(lat_deg)))
    coslat = max(0.1, coslat)
    return m / (111_320.0 * coslat)


def scale_back_to_valid_geo(
    old_lat: float,
    old_lon: float,
    new_lat: float,
    new_lon: float,
) -> Tuple[float, float]:
    """
    Clamp a transformed coordinate back within valid lat/lon bounds.

    If the displacement from `(old_lat, old_lon)` to `(new_lat,
    new_lon)` would push the point past ±89.9° latitude or ±179.9°
    longitude, uniformly scales the whole (dlat, dlon) displacement
    vector down (never below 0) so the result lands exactly on the
    nearest exceeded limit instead of clipping each axis
    independently, preserving the direction of the shift.

    Parameters
    ----------
    old_lat : float
        Original latitude in degrees, before transformation.
    old_lon : float
        Original longitude in degrees, before transformation.
    new_lat : float
        Transformed latitude in degrees, possibly out of bounds.
    new_lon : float
        Transformed longitude in degrees, possibly out of bounds.

    Returns
    -------
    Tuple[float, float]
        The (lat, lon) pair, scaled back within valid bounds if needed.
    """
    lat_limit = 89.9
    lon_limit = 179.9
    dlat = new_lat - old_lat
    dlon = new_lon - old_lon
    scale = 1.0
    if dlat > 0 and new_lat > lat_limit:
        scale = min(scale, (lat_limit - old_lat) / dlat)
    elif dlat < 0 and new_lat < -lat_limit:
        scale = min(scale, (-lat_limit - old_lat) / dlat)
    if dlon > 0 and new_lon > lon_limit:
        scale = min(scale, (lon_limit - old_lon) / dlon)
    elif dlon < 0 and new_lon < -lon_limit:
        scale = min(scale, (-lon_limit - old_lon) / dlon)
    scale = max(0.0, scale)
    return old_lat + scale * dlat, old_lon + scale * dlon


def get_hash_str(seed: str, tag: str, length: int = 64) -> str:
    """
    Derive a deterministic hash 256 from a seed and a tag string and
    restrict the length.

    Parameters
    ----------
    seed : str
        Seed value that makes the result reproducible.
    tag : str
        Label identifying which derived quantity this is for.
    length: int
        the length restriction for the string length

    Returns
    -------
    float
        A value in [0, 1).
    """
    payload = f"{seed}|{tag}"
    h = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return h[:length]


def get_hash_float(seed: str, tag: str) -> float:
    """
    Derive a deterministic float in [0, 1) from a seed and a tag string.

    Parameters
    ----------
    seed : str
        Seed value that makes the result reproducible.
    tag : str
        Label identifying which derived quantity this is for.

    Returns
    -------
    float
        A value in [0, 1).
    """
    hash_str = get_hash_str(seed, tag, length=16)
    return (int(hash_str[:16], 16) % 10_000_000) / 10_000_000.0


def has_suffix(full: str) -> bool:
    """
    Checks isf the string as a suffix (.txt for example). By going through the string in reverse
    order and checking if the character is a dot. If a backslash comes before the dot it is
    considered a folder.
    """
    for char in reversed(full):
        if char == ".":
            return True
        elif char == "\\":
            return False
    raise AttributeError("Full Objectname is neither Folder or Object.")


# ----------- Courtesy of Claude ------------------------------------------
def format_float(x: float) -> str:
    """
    Format a float without a redundant ".0" or leading zeros in the exponent.

    Parameters
    ----------
    x : float
        The value to format.

    Returns
    -------
    str
        The formatted string, e.g. "3" instead of "3.0", or "1e-5"
        instead of "1.0e-05".
    """
    s = str(x)
    # e-05 -> e-5, e+08 -> e+8 (führende Nullen im Exponenten entfernen)
    s = re.sub(r"([eE][+-])0+(\d)", r"\1\2", s)
    # 3.0 -> 3, 1.0e-5 -> 1e-5 (überflüssiges ".0" entfernen)
    s = re.sub(r"\.0(?=$|[eE])", "", s)
    return s


# -------------------------------------------------------------------------


def get_alteration(
    anonymizer: SeededNameAnonymizer,
    name: str,
    current_id: str,
):
    """
    Get the alteration factor for a given name and id. The alteration factor is a random number
    """
    random_alteration = get_hash_float(
        seed=anonymizer.seed,
        tag=f"impedance_alteration_{name}_{current_id}",
    )
    alteration = (
        1.0 + (random_alteration - 0.5) * 2 * anonymizer.alteration_factor / 100
    )
    return alteration


# ---------------------------- test helpers -------------------------------


def get_test_files(
    filename: str, suffix: str, folder_name: str, param_list: List | None = None
) -> Tuple[Path, Path, Path, Path]:
    """
    Build the original, anonymized, restored, and mapping file paths for a test case.

    The anonymized/restored/mapping filenames are suffixed with the
    stringified `param_list` entries (parametrization flags), so that
    different parameter combinations for the same base `filename` get
    distinct, non-colliding paths under `test/test_data/<folder_name>`.

    Parameters
    ----------
    filename : str
        Base name of the original test file (without suffix).
    suffix : str
        File extension to append, including the dot (e.g. ".csv").
    folder_name : str
        Subfolder under `test/test_data` for this format (e.g. "CSV").
    param_list : List | None
        Parametrization values used to disambiguate generated filenames.

    Returns
    -------
    Tuple[Path, Path, Path, Path]
        The (orig_file, anym_file, restore_file, mapping_file) paths.
    """
    created_file_name = filename
    for param in param_list:
        if param is None:
            created_file_name += "None"
        else:
            created_file_name += str(param)

    project_dir = Path(__file__).parent.parent.resolve()
    test_dir = Path(project_dir, "test")
    data_dir = Path(test_dir, "test_data", folder_name)
    orig_file = Path(data_dir, "orig", filename + suffix)
    anym_file = Path(data_dir, "anym", created_file_name + suffix)
    restore_file = Path(data_dir, "restore", created_file_name + suffix)
    mapping_file = Path(data_dir, "mapping", created_file_name + ".json")

    return orig_file, anym_file, restore_file, mapping_file


def delete_test_data(file_list: List[Path]):
    """
    Delete the anonymized, restored, and mapping files produced by a test run.

    Parameters
    ----------
    anym_file : Path
        Path to the anonymized file to remove.
    restore_file : Path
        Path to the restored file to remove.
    mapping_file : Path
        Path to the mapping JSON file to remove.
    """
    for file in file_list:
        file.unlink()
