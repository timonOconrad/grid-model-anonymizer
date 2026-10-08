"""
anym_pf.py - PowerFactory (.pfd) anonymizer
============================================

Anonymizes a DIgSILENT PowerFactory project (.pfd) in place via the
PowerFactory Python API, using the same seed-based deterministic
approach and mapping JSON shared with anym_cgmes / anym_csv / anym_json.

Workflow
--------
1. Locate the installed PowerFactory version and append its Python
   API path (`<PF install>\\Python\\<major.minor>`) to sys.path before
   `import powerfactory` is attempted.
2. Import the source .pfd into a temporary PowerFactory project.
3. Walk all relevant network objects (elements, types, switches,
   cubicles, graphics) and:
   - anonymize loc_name and a fixed set of string attributes
     (sernum, constr, chr_name, dar_src, manuf, for_name,
     foreignKey) via deterministic token substitution,
   - anonymize or delete (`desc=True`) object descriptions,
   - transform+jitter or delete (`gps=True`) GPS coordinates,
   - optionally remap CIM RDF identifiers.
4. Export the anonymized project back out as a .pfd and write a
   mapping JSON recording every original -> anonymized value, which
   is later used to fully reverse the process (`run_powerfactory_restore`).

Design rationale
----------------
- All renames/attribute writes go through `safe_set` / `_get_*_attr`
  helpers that tolerate objects which don't support a given attribute
  (PowerFactory classes are heterogeneous), so a missing attribute on
  one object type never aborts the whole run.
- Bulk operations are wrapped in `pf_bulk_mode_begin/_end` to disable
  GUI/progress-bar updates and enable the write cache, which is
  required for acceptable performance on larger projects.
- GPS anonymization runs as a second pass, after names/cimRdfIds have
  already been changed, so the mapping can key GPS records by the
  *original* cimRdfId / full path even though the object itself has
  since been renamed.
- Restoring is the import/export workflow run with the mapping JSON
  applied in reverse: deleted-GPS records are restored first (while
  identifiers are still resolvable), then loc_name/attributes/desc,
  then cimRdfId, then transformed-GPS records last.

Requires a local PowerFactory installation (searches
`C:\\Program Files\\DIgSILENT` and `C:\\Program Files (x86)\\DIgSILENT`)
and a matching PowerFactory-compatible Python interpreter version;
exits with an error message at import time if no compatible version
is found. Depends on: psutil, pf_utils, utils (SeededNameAnonymizer etc.).
"""

# from __future__ import annotations

import logging
import math
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from utils import pf_utils, utils

# the pf module has to be imported from this function to ensure it is the
# correct version
pf = pf_utils.import_powerfactory_module()

logger = logging.getLogger(" anym_pf.py")


# ----------------------------
# Anonymize primitives
# ----------------------------
def anonymize_cim_rdf_id(
    obj, seed: str, anonymizer: utils.SeededNameAnonymizer
) -> None:
    """
    Deterministically remap an object's cimRdfId and record the mapping.

    No-op if the object has no cimRdfId. Reuses an existing mapping
    entry if this ID was already remapped.

    Parameters
    ----------
    obj : the PF object whose CIM RDF ID should be remapped.
    seed : str
        Seed value passed to `utils.generate_seeded_uuid` to make the
        new ID deterministic and reproducible.
    anonymizer : utils.SeededNameAnonymizer
        Anonymizer instance whose `cim_forward` mapping is read from
        and updated.

    Returns
    -------
    None
        No-op (returns without effect) if the object has no cimRdfId.

    """
    ids = pf_utils.get_cim_rdf_id(obj)
    if not ids:
        return

    old_id = ids[0]
    if old_id in anonymizer.cim_forward:
        new_id = anonymizer.cim_forward[old_id]
    else:
        new_id = utils.generate_seeded_uuid(old_id, seed)
        anonymizer.cim_forward[old_id] = new_id

    pf_utils.set_cim_rdf_id(obj, new_id)


def anonymize_string_fields(
    obj,
    *,
    anonymizer: utils.SeededNameAnonymizer,
    fields: List[str],
    empty_as_zero: bool = True,
):
    """
    Anonymize a fixed set of string attributes on a PF object in place.

    For each field in `fields`: skips fields that don't exist; if the
    value is empty and `empty_as_zero` is True, substitutes a value
    derived from the object's pid_/oid_ before translating (so empty
    fields still get a deterministic anonymized value); otherwise
    leaves genuinely empty fields untouched.

    Parameters
    ----------
    obj : the PF object whose attributes should be anonymized.
    anonymizer : utils.SeededNameAnonymizer
        Anonymizer used to derive the new attribute values.
    fields : List[str]
        Names of the string attributes to anonymize (e.g. "sernum",
        "manuf").
    empty_as_zero : bool, optional
        If True (default), empty values are replaced with a
        deterministic non-empty placeholder derived from the object's
        `pid_`/`oid_` before anonymizing, instead of being skipped.
    """
    for attr in fields:
        old = pf_utils.get_str_attr(obj, attr)
        if old is None:
            continue

        old_s = str(old).strip()

        if not old_s and empty_as_zero:
            old_s = str(int(obj.pid_) + int(obj.oid_))
        elif not old_s:
            continue

        new_s = anonymizer.translate_attr(attr, old_s)

        if new_s != old_s:
            pf_utils.set_str_attr(obj, attr, new_s)


# ----------------------------
# GPS handling
# ----------------------------
def _gps_apply_and_record(
    obj,
    *,
    seed: str,
    gps_delete: bool,
    gps_transform,
    anonymizer: utils.SeededNameAnonymizer,
    orig_cim_id: Optional[str],
    orig_loc_name_for_jitter: Optional[str],
):
    """
    Apply GPS anonymization to one object and record it for later reversal.

    Second-pass helper, run after loc_name/cimRdfId have already been
    changed, so `orig_cim_id` is used as the mapping key instead of
    the object's (now changed) current CIM ID. No-op if `orig_cim_id`
    is None, if the object has no readable GPS coordinates, or if its
    coordinates are already at the origin (0, 0).

    Parameters
    ----------
    obj : the PF object whose GPS coordinates should be anonymized.
    seed : str
        Seed value used to derive the deterministic jitter offset.
    gps_delete : bool
        True to zero out GPS coordinates (delete mode); False to
        transform and jitter them instead.
    gps_transform : callable
        A `(lat, lon) -> (lat, lon)` function, as returned by
        `utils.build_geo_transform`, applied when `gps_delete` is
        False.
    anonymizer : utils.SeededNameAnonymizer
        Anonymizer whose `gps_mapping` is updated with the
        before/after (or before/deleted) GPS record.
    orig_cim_id : Optional[str]
        The object's CIM RDF ID *before* any anonymization, used as
        the mapping key. If None, the function returns without doing
        anything.
    orig_loc_name_for_jitter : Optional[str]
        The object's original `loc_name` (or None to fall back to its
        current `loc_name`), used only to seed the per-object jitter
        deterministically.
    """
    if orig_cim_id is None:
        return

    old_lat = pf_utils.get_float_attr(obj, "GPSlat")
    old_lon = pf_utils.get_float_attr(obj, "GPSlon")
    if old_lat is None or old_lon is None:
        return

    if abs(old_lat) < 1e-12 and abs(old_lon) < 1e-12:
        return

    try:
        logger.debug("GPS coordinates: %s", obj.GPScoords)
    except AttributeError:
        pass

    try:
        obj.GPScoords = [[0.0, 0.0] for _ in obj.GPScoords]
    except AttributeError:
        pass

    if gps_delete:
        cur_ids = pf_utils.get_cim_rdf_id(obj)
        cim_after = cur_ids[0] if cur_ids else None

        anonymizer.gps_mapping.setdefault(
            orig_cim_id,
            {
                "old": [float(old_lat), float(old_lon)],
                "deleted": True,
                "cim_after": cim_after,
                "full_name_after": pf_utils.get_full_name(obj),
            },
        )
        pf_utils.safe_set(obj, "GPSlat", 0.0, verbose=False)
        pf_utils.safe_set(obj, "GPSlon", 0.0, verbose=False)
        return

    new_lat, new_lon = gps_transform(old_lat, old_lon)

    base_name = orig_loc_name_for_jitter or pf_utils.get_loc_name(obj)
    jitter_m = 100.0

    r = utils.get_hash_float(seed, f"gps_jitter_r|{base_name}") * jitter_m
    theta = 2.0 * math.pi * utils.get_hash_float(seed, f"gps_jitter_theta|{base_name}")

    dx_m = r * math.cos(theta)
    dy_m = r * math.sin(theta)

    dlat = utils.meters_to_deg_lat(dy_m)
    dlon = utils.meters_to_deg_lon(dx_m, new_lat)

    new_lat += dlat
    new_lon += dlon

    new_lat, new_lon = utils.scale_back_to_valid_geo(old_lat, old_lon, new_lat, new_lon)

    anonymizer.gps_mapping.setdefault(
        orig_cim_id,
        {
            "old": [float(old_lat), float(old_lon)],
            "new": [float(new_lat), float(new_lon)],
        },
    )

    pf_utils.safe_set(obj, "GPSlat", float(new_lat), verbose=False)
    pf_utils.safe_set(obj, "GPSlon", float(new_lon), verbose=False)


# ----------------------------
# Line Resetting Handlers
# ----------------------------


def set_impedances(
    old_type: object,
    new_type: object,
    ratio: float,
    anonymizer: utils.SeededNameAnonymizer,
    ln_name: str,
) -> None:
    """
    Scale a line type's per-km impedance values onto a new line type.
    Also add Impedance alteration for further anonymization

    Parameters
    ----------
    old_type : line type object
        The original line type to read per-km impedance values from.
    new_type : line type object
        The newly created line type (see `create_new_line_type`) to
        write the rescaled impedance values to.
    ratio : float
        Scaling factor applied to each impedance value, typically
        `old_length / new_length`.
    anonymizer : utils.SeededNameAnonymizer
        Anonymizer providing the seed used to derive the deterministic
        alteration factor.
    ln_name : str
        Name of the original line type, mixed into the alteration
        seed so different lines get independent alteration factors.
    """

    for impedance_type in pf_utils.IMPEDANCE_TYPES:
        impedance_value_per_km = pf_utils.get_float_attr(old_type, impedance_type)
        # check if there actually is an impedance
        if impedance_value_per_km is None:
            return

        # set the alteration
        alteration = utils.get_alteration(
            anonymizer=anonymizer,
            name=impedance_type,
            current_id=ln_name,
        )

        new_impedance_per_km = impedance_value_per_km * alteration * ratio

        pf_utils.safe_set(
            new_type, impedance_type, float(new_impedance_per_km), verbose=False
        )


def set_line_length(obj: object, anonymizer: utils.SeededNameAnonymizer) -> None:
    """
    reset the line lengths and the new line type and storing it in the anonymizer
    for the mapping

    Parameters
    ----------
    obj: Line object
        the line object (not the line type object)
    anonymizer : utils.SeededNameAnonymizer
            Anonymizer providing the seed used to derive the deterministic
            alteration factor.
    """

    # check if the object is a power line and actually needs length resetting
    obj_name = pf_utils.get_loc_name(obj)
    new_name = obj_name + "LineType"
    old_len = pf_utils.get_float_attr(obj, "dline")
    if old_len is None:
        return
    # create the new line type from old one
    try:
        ln_type = obj.GetType()
    except AttributeError:
        ln_type = obj.GetAttribute("typ_id")

    if ln_type is None:
        # some lines are just clutches without relevant impedance
        # that are skipped here
        if obj.GetAttribute("c_ptow"):
            return
        # some lines are divided into segments, if this is the case
        # then the line setting will be done in the segment and the
        # total line can be skipped
        try:
            c = obj.GetChildren(0)
            fullname = pf_utils.get_full_name(c[0])
            if fullname.endswith("ElmLnesec"):
                return
            else:
                logger.debug("Odd Child of Line Element: %s", fullname)
        except AttributeError as e:
            raise AttributeError from e

    ln_name = pf_utils.get_loc_name(ln_type)
    try:
        new_type = create_new_line_type(ln_type, new_name)
        # catches Line Types that are read-only and can not be altered
        if new_type is None:
            return
        ratio = old_len
        # reset the impedance, since the new line length is always 1 km the ratio = old length
        set_impedances(ln_type, new_type, ratio, anonymizer, ln_name)

        # save the new line in the anonymizer
        anonymizer.line_mapping.setdefault(
            new_name,
            {
                "name": ln_name,
                "length": old_len,
            },
        )
        # reset the line data
        pf_utils.safe_set(obj, "dline", float(1), verbose=False)
        pf_utils.safe_set(obj, "typ_id", new_type, verbose=False)
    except AttributeError as e:
        raise AttributeError from e


def create_new_line_type(old_type, new_name: str):
    """
    create a new line type object as a copy of the old line type

    Parameters
    ----------
    old_type : line type object
        The old line type
    new_name : string
        The name of the new line type object

    Returns
    -------
    new_type : line type object
        The new line type
    """
    try:
        parent = old_type.GetParent()
        new_type = parent.AddCopy(old_type, new_name)
    except AttributeError as e:
        raise AttributeError from e
    return new_type


# ----------------------------
# Time Anonymization
# ----------------------------


def anonymize_time(obj, anonymizer: utils.SeededNameAnonymizer):
    """
    Set a new anonymized time for powerfactory object.

    Parameters
    ----------
    obj : type object
        The powerfactory object to be time updated
    anonymizer : anonymizer
        The anonymizer object used for the anonymization
    """
    old_time = int(pf_utils.get_float_attr(obj, "iStudyTime"))
    new_time = anonymizer.add_time(old_time)
    pf_utils.safe_set(obj, "iStudyTime", new_time)


# ----------------------------
# Full anonymize procedure
# ----------------------------
def anonymize_objects(
    app,
    objects: List,
    seed: str,
    anonymizer: utils.SeededNameAnonymizer,
    desc: bool,
    gps: bool,
    remap_ids: bool,
) -> None:
    """
    Anonymize a collected list of PF objects in place and return the mapping.

    Parameters
    ----------
    app : the PowerFactory application object (from pf.GetApplication()).
    objects : List
        The PF objects to anonymize, as returned by
        `pf_utils.collect_unique_objects_for_anonymization`.
    seed : str
        Seed value that makes the whole anonymization run
        deterministic and reproducible;
    desc : bool
        Passed through to `pf_utils.sanitize_desc`: True to delete
        descriptions, False to anonymize their contents in place
        (exact behavior defined by that function).
    gps : bool
        Passed through to `_gps_apply_and_record` as `gps_delete`:
        True to delete GPS coordinates, False to transform + jitter
        them instead.
    prefix : str, optional
        Prefix for anonymized string tokens (default "ANON_")
    length : int, optional
        Initial hash length for anonymized tokens (default 10)

    Returns
    -------
    utils.SeededNameAnonymizer
        The anonymizer instance holding every mapping created during
        this run (names/attributes, CIM RDF IDs, GPS, line lengths,
        study-case times), ready to be persisted via
        `utils.save_mapping_json`.
    """
    gps_transform = utils.build_geo_transform(seed)

    # Store original keys for the second pass:
    # python object id -> (orig_cim_id, orig_loc_name)
    orig_keys: Dict[int, Tuple[Optional[str], Optional[str]]] = {}

    pf_utils.pf_bulk_mode_begin(app)

    try:
        for obj in objects:
            full = obj.GetFullName()
            if full.endswith(".SetPrj"):
                anonymizer.project_unit_system, anonymizer.project_unit = (
                    pf_utils.set_project_unit(obj)
                )
                break
    finally:
        pf_utils.pf_bulk_mode_end(app)

    pf_utils.pf_bulk_mode_begin(app)
    try:
        for obj in objects:
            full = obj.GetFullName()
            if not full:
                continue
            if full.endswith(".IntCase"):
                anonymize_time(obj, anonymizer)
                continue

            if (
                full.endswith(".IntPrj")
                or full.endswith(".IntUser")
                or full.startswith(r"\Lib.IntLibrary")
                or full.endswith(".IntFltcases")
                or full.endswith(".SetPrj")
                or not utils.has_suffix(full)
            ):
                continue

            if full.endswith(".ElmLne"):
                obj.GPScoords = [[0.0, 0.0]]  # [[0.0, 0.0] for _ in obj.GPScoords]

            ids = pf_utils.get_cim_rdf_id(obj)
            orig_cim = ids[0] if ids else None
            orig_loc = pf_utils.get_loc_name(obj)

            orig_keys[id(obj)] = (orig_cim, orig_loc)

            anonymize_string_fields(
                obj,
                anonymizer=anonymizer,
                fields=[
                    "sernum",
                    "constr",
                    "chr_name",
                    "dar_src",
                    "manuf",
                    "for_name",
                    "foreignKey",
                ],
                empty_as_zero=True,
            )

            if remap_ids:
                anonymize_cim_rdf_id(obj, seed, anonymizer)

            pf_utils.sanitize_desc(obj, desc, anonymizer)

            # loc_name
            if isinstance(orig_loc, str) and orig_loc.strip():
                new_name = anonymizer.translate(orig_loc)
                if new_name != orig_loc:
                    pf_utils.make_unique_if_needed(obj, new_name, anonymizer)

    finally:
        pf_utils.pf_bulk_mode_end(app)

    pf_utils.pf_bulk_mode_begin(app)
    try:
        for obj in objects:
            full = obj.GetFullName()
            if (
                full.endswith(".IntPrj")
                or full.endswith(".IntUser")
                or full.endswith(".SetPrj")
                or full == ""
                or full is None
            ):
                continue
            orig_cim, orig_loc = orig_keys.get(id(obj), (None, None))
            _gps_apply_and_record(
                obj,
                seed=seed,
                gps_delete=gps,
                gps_transform=gps_transform,
                anonymizer=anonymizer,
                orig_cim_id=orig_cim,
                orig_loc_name_for_jitter=orig_loc,
            )
            set_line_length(obj, anonymizer=anonymizer)
    finally:
        pf_utils.pf_bulk_mode_end(app)


# ----------------------------
# Public entrypoints
# ----------------------------
def run_powerfactory_import_export(
    in_path: Path,
    out_path: Path,
    random_seed: str,
    mapping_out_path: Path,
    desc: bool,
    gps: bool,
    remap_ids: bool,
    anonymizer: utils.SeededNameAnonymizer,
):
    """
    End-to-end PF anonymization: import .pfd -> anonymize -> export .pfd.

    Parameters
    ----------
    in_path : Path
        Path to the source .pfd file to anonymize.
    out_path : Path
        Destination path for the anonymized .pfd export. Parent
        directories are created if needed.
    random_seed : str
        Seed value that makes the whole anonymization run
        deterministic and reproducible;
    mapping_out_path : Path
        Destination path for the mapping JSON that records every
        original -> anonymized value, needed later to reverse the
        anonymization.
    desc : bool
        True to delete object descriptions, False to anonymize their
        contents in place;
    gps : bool
        True to delete GPS coordinates (set to 0/0), False to
        transform and jitter them instead; The mapping JSON records the
        original lat/lon in both cases (plus the post-anonymization CIM ID
        and full name when deleted, or the new lat/lon when transformed).
    prefix : str, optional
        Prefix for anonymized string tokens (default "ANON_").
    hash_length : int, optional
        Initial hash length for anonymized tokens (default 10).

    Raises
    ------
    RuntimeError
        If the PowerFactory Python API or application is not
        available.
    FileNotFoundError
        If `in_path` does not exist.

    Notes
    -----
    Export failures (`OSError`, `RuntimeError` from
    `pf_utils.export_project_to_pfd`) are logged but not re-raised, so
    the mapping JSON is still written even if the final export step
    fails.
    """
    if pf is None:
        logger.error("PowerFactory Python API not available. Cannot run.")
        raise RuntimeError("PowerFactory Python API not available.")

    in_path = Path(in_path)
    out_path = Path(out_path)
    mapping_out_path = Path(mapping_out_path)

    pf_utils.kill_powerfactory()

    app = pf.GetApplication()
    if not app:
        raise RuntimeError(
            "PowerFactory Application not available (pf.GetApplication() returned None)."
        )

    app.ClearOutputWindow()
    logger.info("=== anym_PF.py: Start Import/Anonymize/Export ===")

    if not in_path.exists():
        raise FileNotFoundError(f"Input PFD not found: {in_path}")

    project_name = in_path.stem

    pf_utils.delete_project_if_exists(app, project_name)
    pf_utils.import_pfd_into_current_user(app, in_path)
    pf_utils.activate_project(app, project_name)

    gridtocim = app.GetFromStudyCase("ComGridtocim")
    if gridtocim:
        gridtocim.AssignCimRdfIds()

    objects = pf_utils.collect_unique_objects_for_anonymization(app)
    logger.info("Objects to anonymize (unique): %d", len(objects))

    anonymize_objects(
        app=app,
        objects=objects,
        seed=random_seed,
        anonymizer=anonymizer,
        desc=desc,
        gps=gps,
        remap_ids=remap_ids,
    )

    utils.save_mapping_json(mapping_out_path, anonymizer)
    logger.info("Mapping saved: %s", mapping_out_path)

    try:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        pf_utils.export_project_to_pfd(app, out_path)
        logger.info("Export written: %s", out_path)
    except OSError as e:
        logger.warning("Export not executed: %s", e)
    except RuntimeError as e:
        logger.error("Export failed: %s", e)
    try:
        check_load_flow_accuracy(
            in_path, out_path, mapping_out_path, anonymizer.alteration_factor
        )
    except AttributeError as e:
        logger.error("Load flow Analysis not possible. Error Message: %s", e)


def check_load_flow_accuracy(
    orig_path: Path, anym_path: Path, mapping_out_path: Path, alt_factor: float
):
    """Checks if the load flow results of the original and anonymized project are within the
    expected range. Give a warning if the averaged error is larger than 1% and log the maximum
    deviation of the load flow results.
    """
    _, anon_rev, _, _, _, _, prefix, _, _ = utils.get_mappings(mapping_out_path)

    app = pf.GetApplication()
    orig_ldf_results = pf_utils.get_load_flow_results(app, orig_path, anon_rev, prefix)
    anym_ldf_results = pf_utils.get_load_flow_results(
        app, anym_path, anon_rev, prefix, project_name=orig_path.stem
    )

    difference_list = []
    for type_key, type_entry in orig_ldf_results.items():
        for elem_key, elem_entry in type_entry.items():

            for value_key, orig_value_entry in elem_entry.items():
                if anym_ldf_results[type_key][elem_key] == "Unknown":
                    continue
                anym_value_entry = anym_ldf_results[type_key][elem_key][value_key]
                try:
                    rel_error = (orig_value_entry - anym_value_entry) / orig_value_entry
                except ZeroDivisionError:
                    rel_error = orig_value_entry - anym_value_entry
                difference_list.append(rel_error)

    square_error = [x**2 for x in difference_list]
    mean_square_error = sum(square_error) / len(square_error)

    rmse = math.sqrt(mean_square_error)
    max_error = math.sqrt(max(square_error))
    logger.info(
        "The maximum deviation of the load flow results is %0.2f%% in one of the elements, "
        "the Alteration Factor is at %s%%.",
        max_error * 100,
        alt_factor,
    )
    if rmse >= 1 / 100:
        logger.warning(
            "The averaged error for load flow analysis is larger than 1%% with %0.2f%%! "
            "Use a smaller alteration factor to reduce the error.",
            rmse * 100,
        )
    else:
        logger.warning(
            "The averaged error for a load flow analysis is at %0.2f%%!",
            rmse * 100,
        )
