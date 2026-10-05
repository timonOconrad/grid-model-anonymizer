"""
restore_pf.py
==============

Reverses a previously anonymized PowerFactory project back to its
original values, using the mapping JSON produced during
anonymization.

Given an anonymized .pfd project file and the corresponding mapping
file, this module imports the project into PowerFactory and restores,
in order: line types/lengths and GPS coordinates for deleted objects
(`restore_line_type`, `restore_gps`), object names/attributes/
descriptions/CIM RDF IDs via the unified anonymization mapping, GPS
coordinates for objects that still exist under their (restored) CIM
ID, and study-case timestamps (`restore_times`), before exporting the
restored project back to .pfd via the public
`run_powerfactory_restore` entrypoint.
"""

import logging
import re
from pathlib import Path
from typing import Dict, List, Tuple

from utils import pf_utils, utils

pf = pf_utils.import_powerfactory_module()
logger = logging.getLogger("restore_pf.py")

FIELDS = [
    "sernum",
    "constr",
    "chr_name",
    "dar_src",
    "manuf",
    "for_name",
    "foreignKey",
]

# ----------------------------
# Restore procedure (UNIFIED)
# ----------------------------


_ANON_RE = re.compile(r"\bANON_[0-9A-F]{6,}\b")  # 6+ damit auch längere Hashes gehen


def make_obj_dict(objects: List) -> Dict[str, object]:
    """
    Create a Dictionary from a list of objects with a clear key
    to make searching for certain objects easier

    Parameters
    ----------
    objects: List
        The object list

    Returns
    -------
    objects_dict: Dict
        The object list as a dictionary
    """
    objects_dict: Dict[str, object] = {}

    for obj in objects:
        # since one loc_name can be given to multiple loc names
        # the obj_class is added to the key
        obj_name = pf_utils.get_loc_name(obj)
        obj_class = obj.GetClassName()
        obj_key = obj_name + "." + obj_class
        objects_dict[obj_key] = obj
    return objects_dict


def restore_anon_tokens_in_text(text: str, anon_rev: Dict[str, str]) -> str:
    """
    Replace every ANON_<hash> token found in free text with its
    original value from `anon_rev`, leaving unrecognized tokens as-is.
    """

    def repl(m: re.Match) -> str:
        tok = m.group(0)
        return anon_rev.get(tok, tok)

    return _ANON_RE.sub(repl, text)


def get_old_coordinates(coordinates_map: dict[str, dict]) -> Tuple[float, float]:
    """
    The restoration of the gps data in a function. This represents
    the first iteration of the gps restoration, that handles that only applies
    if the gps data was deleted

    Parameters
    ----------
    coordinates_map: Dict[str, Dict]
        The mapping of the gps data, for one Data Point. The keys are "new"
        and "old" for the old (before anonymization) and new (after
        anonymization) coordinates of the
    Returns
    -------
    old_lat, old_lon: float
        The Latitude and Longitude before the Anonymization
    """
    old = coordinates_map.get("old")
    if not (isinstance(old, list) and len(old) == 2):
        return None
    old_lat, old_lon = float(old[0]), float(old[1])
    return old_lat, old_lon


def get_obj_by_full_name(coordinates_map: dict[str, dict], app) -> object:
    """
    The restoration of the gps data in a function. This represents
    the first iteration of the gps restoration, that handles that only applies
    if the gps data was deleted

    Parameters
    ----------
    coordinates_map: Dict[str, Dict]
        The mapping of the gps data, for one Data Point. The keys are "new"
        and "old" for the old (before anonymization) and new (after
        anonymization) coordinates of the
    app: object
        The Powerfactory Application

    Returns
    -------
    target: float
        The PowerFactory Element object, referenced in coordinates map
    """
    fn = coordinates_map.get("full_name_after")
    if isinstance(fn, str) and fn:
        target = pf_utils.search_by_full_name_after(app, fn)
        return target


def restore_gps_from_deletion(
    app,
    gps_map: Dict[str, Dict],
    cim_index_current: Dict[str, object],
    cim_map: Dict[str, str],
) -> None:
    """
    The restoration of the gps data in a function. This represents
    the first iteration of the gps restoration, that handles that only applies if
    the gps data was deleted

    Parameters
    ----------
    app: object
        The Powerfactory Application
    gps_map: Dict[str, Dict]
        The mapping of the gps data. The key is the cim reference and
        data is a dictionary with old and new gps coordinates
    cim_index_current: Dict[str, object]
        The Cim References corresponding to each object.
    cim_map: Dict[str, str]
        The cim mapping with the old and new cim reference
    """
    for orig_cim, coordinates_map in gps_map.items():
        if not coordinates_map.get("deleted", False):
            continue

        old_lat, old_lon = get_old_coordinates(coordinates_map)
        if old_lat is None:
            continue

        target = None

        cim_after = coordinates_map.get("cim_after")
        if isinstance(cim_after, str) and cim_after:
            target = cim_index_current.get(cim_after)

        if target is None:
            current_cim = cim_map.get(orig_cim)
            if isinstance(current_cim, str) and current_cim:
                target = cim_index_current.get(current_cim)

        if target is None:
            target = get_obj_by_full_name(coordinates_map, app)

        if target is None:
            logger.warning("Deleted-GPS target not found (orig_cim=%s)", orig_cim)
            continue

        pf_utils.safe_set(target, "GPSlat", old_lat, verbose=False)
        pf_utils.safe_set(target, "GPSlon", old_lon, verbose=False)


def restore_gps_after_obscuring(
    app, gps_map: Dict[str, Dict], cim_index_orig: Dict[str, object]
):
    """
    The restoration of the gps data in a function. This represents
    the firsecondst iteration of the gps restoration, that handles obscured
    gps data

    Parameters
    app: PowerFactory Application

    gps_map: Dict[str, Dict]
        The mapping of the gps data. The key is the cim reference and
        data is a dictionary with old and new gps coordinates
    cim_index_orig: Dict[str, object]
        The Cim References corresponding to each object.
    """

    for orig_cim, rec in gps_map.items():
        if rec.get("deleted", False):
            continue

        try:
            old_lat, old_lon = get_old_coordinates(rec)
        except AttributeError:
            continue

        target = cim_index_orig.get(orig_cim)
        if target is None:
            fn = rec.get("full_name_after")
            if isinstance(fn, str) and fn:
                target = pf_utils.search_by_full_name_after(app, fn)

        if target is None:
            continue

        pf_utils.safe_set(target, "GPSlat", old_lat, verbose=False)
        pf_utils.safe_set(target, "GPSlon", old_lon, verbose=False)


def restore_times(objects: List, time_rev: Dict):
    """
    A function to iterate through all objects. If they are an "IntCase" they are being restored.

    Parameters
    ----------
    objects : list of objects
        list of all objects in the project
    time_rev : Dict
        Dict of the anonymized times and their original counterparts
    """
    for obj in objects:
        full = pf_utils.get_full_name(obj)
        if full.endswith("IntCase"):
            anym_time = int(pf_utils.get_float_attr(obj, "iStudyTime"))
            orig_time = int(time_rev[str(anym_time)])
            pf_utils.safe_set(obj, "iStudyTime", orig_time, verbose=False)


def get_all_line_types(objects_dict: Dict[str, object]) -> Dict[str, object]:
    """
    Since power Factory only gives the line types, that are currently used in a
    project, this function combines all the unused and used types to a new line type dictionary.

    Parameters
    ----------
    objects_dict: Dict[str, object]
        The objects dictionary with every used object

    Returns
    -------
    all_types_dict: Dict[str, object]
        A Dictionary that contains all the line type objects used and unused
    """
    for obj_key, obj in objects_dict.items():
        # only one instance of "TypLne" is necessary, since we can find all the other "TypLne"
        # with the "GetParent" and "GetChildren" command.
        if obj_key.endswith("LineType.TypLne"):
            type_library = obj.GetParent()
            all_types = type_library.GetChildren(1)
            all_types_dict = make_obj_dict(all_types)
            return all_types_dict

    logger.info("No Line Type Setting was executed. No Resetting Necessary")
    return


def restore_line_type(
    objects_dict: Dict[str, object], line_map: Dict[str, str]
) -> None:
    """
    Restoring all old line types, line lengths and impedances

    Parameters
    ----------
    objects_dict: Dict[str, object]
        The dictionary with all objects and a clear key
    line_rev:  Dict[str, str]
        The reverse mapping with all the new anonymious linetype names as key
        and old lines as data
    """

    all_types = get_all_line_types(objects_dict)
    try:
        for ln_type_key, ln_type_obj in all_types.items():

            if ln_type_key.endswith("LineType.TypLne"):

                # get all the data about the line and line type
                anon_line_name = ln_type_key[:15]

                line_map_key = ln_type_key[:23]
                orig_type = line_map[line_map_key]

                anon_line_key = anon_line_name + ".ElmLne"
                line_obj = objects_dict[anon_line_key]

                orig_type_key = orig_type["name"] + ".TypLne"
                orig_type_obj = all_types[orig_type_key]

                orig_length = orig_type["length"]

                # reset the line information
                pf_utils.safe_set(line_obj, "dline", float(orig_length), verbose=False)
                pf_utils.safe_set(line_obj, "typ_id", orig_type_obj, verbose=False)

                # delete the anon now unused line object
                ln_type_obj.Delete()
    except AttributeError:
        pass


def restore_gps_from_anonymization(
    gps_map: Dict[str, Dict], cim_index_orig: Dict[str, object], app: object
):
    """
    The restoration of the gps data in a function. This represents
    the second iteration of the gps restoration, that only applies if
    the gps data was not deleted

    Parameters
    ----------
    gps_map: Dict[str, Dict]
        The mapping of the gps data. The key is the cim reference and
        data is a dictionary with old and new gps coordinates
    cim_index_orig: Dict[str, object]
        The Cim References corresponding to each object.
    app: object
        The Power Factory application
    """
    for orig_cim, coordinates_map in gps_map.items():
        if coordinates_map.get("deleted", False):
            continue

        old_lat, old_lon = get_old_coordinates(coordinates_map)
        if old_lat is None:
            continue

        target = cim_index_orig.get(orig_cim)
        if target is None:
            target = get_obj_by_full_name(coordinates_map, app)
        if target is None:
            continue

        pf_utils.safe_set(target, "GPSlat", old_lat, verbose=False)
        pf_utils.safe_set(target, "GPSlon", old_lon, verbose=False)


def restore_loc_name(obj, prefix: str, anon_rev: Dict[str, str]):
    """
    Restore all loc_names if they were anonymized

    Parameters
    ----------
    obj : PowerFactory object
        one object in the powerfactory project
    prefix: str
        What prefix was used for anonymized data
    anon_rev : Dict[str, str]
        Dict of the anonymized times and their original counterparts
    """
    # restore loc_name by checking for prefix
    cur_name = pf_utils.get_loc_name(obj)
    if isinstance(cur_name, str) and cur_name.startswith(prefix):
        orig = anon_rev.get(cur_name)
        if orig:
            try:
                obj.SetAttribute("loc_name", orig)
            except AttributeError:
                pass


def restore_string_attributes(obj, prefix, anon_rev):
    """
    Restore all str attributes if they were anonymized

    Parameters
    ----------
    obj : PowerFactory object
        one object in the powerfactory project
    prefix: str
        What prefix was used for anonymized data
    anon_rev : Dict[str, str]
        Dict of the anonymized times and their original counterparts
    """
    for attr in FIELDS:
        cur_val = pf_utils.get_str_attr(obj, attr)
        if cur_val is None:
            continue
        cur_s = str(cur_val).strip()
        if cur_s.startswith(prefix):
            orig = anon_rev.get(cur_s)
            if orig is not None:
                pf_utils.set_str_attr(obj, attr, orig)


def restore_desc(obj, anon_rev):
    """
    Restore all dexcriptions if they were deleted

    Parameters
    ----------
    obj : PowerFactory object
        one object in the powerfactory project
    anon_rev : Dict[str, str]
        Dict of the anonymized times and their original counterparts
    """
    cur_desc = pf_utils.get_str_attr(obj, "desc")
    if cur_desc is not None:
        cur_desc_s = str(cur_desc)
        if cur_desc_s.strip() != "" and cur_desc_s != "Deleted":
            restored = restore_anon_tokens_in_text(cur_desc_s, anon_rev)
            restored = pf_utils.desc_normalize(restored).strip()
            if restored != cur_desc_s:
                pf_utils.safe_set(obj, "desc", restored, verbose=False)


def restore_cim_rdfid(obj, cim_rev):
    """
    Restore all cim RDF IDs, if they were anonymized

    Parameters
    ----------
    obj : PowerFactory object
        one object in the powerfactory project
    cim_rev : Dict[str, str]
        Dict of the anonymized times and their original counterparts
    """
    ids = pf_utils.get_cim_rdf_id(obj)
    if ids:
        cur_id = ids[0]
        if cur_id in cim_rev:
            pf_utils.set_cim_rdf_id(obj, cim_rev[cur_id])


def restore_from_mapping(app, mapping_path: Path):
    """
    Restore inside an already imported project using the mapping JSON:
    - Restore GPS first for deleted=true (while anonymized identifiers are still available)
    - Restore loc_name, attributes, desc using unified anon_mapping (reverse lookup by ANON_*)
    - Restore cimRdfId
    - Restore GPS for transformed cases AFTER cim restore
    """
    (
        line_map,
        anon_rev,
        time_rev,
        cim_rev,
        cim_map,
        gps_map,
        prefix,
    ) = utils.get_mappings(mapping_path)

    objects = pf_utils.collect_unique_objects_for_anonymization(app)
    # ---------------------------------------------------------
    # 1) Restore GPS for deleted=true BEFORE renaming anything and reset lines
    # ---------------------------------------------------------
    cim_index_current = pf_utils.build_cim_index(objects)

    pf_utils.pf_bulk_mode_begin(app)
    try:
        restore_gps_from_deletion(
            app=app,
            gps_map=gps_map,
            cim_index_current=cim_index_current,
            cim_map=cim_map,
        )
        obj_dict = make_obj_dict(objects)
        restore_line_type(obj_dict, line_map)
    finally:
        pf_utils.pf_bulk_mode_end(app)

    # ---------------------------------------------------------
    # 2) Restore loc_name, attributes, desc, cimRdfId
    # ---------------------------------------------------------

    objects = pf_utils.collect_unique_objects_for_anonymization(app)

    pf_utils.pf_bulk_mode_begin(app)
    try:
        for obj in objects:
            restore_loc_name(obj, prefix, anon_rev)
            restore_string_attributes(obj, prefix, anon_rev)
            restore_desc(obj, anon_rev)
            restore_cim_rdfid(obj, cim_rev)
    finally:
        pf_utils.pf_bulk_mode_end(app)

    # ---------------------------------------------------------
    # 3) Restore GPS for transformed cases AFTER cim restore
    # ---------------------------------------------------------
    objects = pf_utils.collect_unique_objects_for_anonymization(app)
    cim_index_orig = pf_utils.build_cim_index(objects)

    pf_utils.pf_bulk_mode_begin(app)
    try:
        restore_gps_from_anonymization(gps_map, cim_index_orig, app)
    finally:
        pf_utils.pf_bulk_mode_end(app)

    # ---------------------------------------------------------
    # 4) Restore the time stamps for each case
    # ---------------------------------------------------------
    pf_utils.pf_bulk_mode_begin(app)
    try:
        restore_times(objects, time_rev)
    finally:
        pf_utils.pf_bulk_mode_end(app)


def run_powerfactory_restore(
    in_path: Path,
    out_path: Path,
    mapping_path: Path,
    project_name: str = None,
):
    """
    Import PFD -> restore from JSON -> export PFD
    """
    if pf is None:
        logger.error(
            "PowerFactory Python API not available (pf is None). Cannot restore."
        )
        raise RuntimeError("PowerFactory Python API not available.")
    logger.info("=== anym_PF.py: Start Reverse ===")
    in_path = Path(in_path)
    out_path = Path(out_path)
    mapping_path = Path(mapping_path)

    pf_utils.kill_powerfactory()

    app = pf.GetApplication()
    if not app:
        raise RuntimeError("PowerFactory Application not available.")

    if not in_path.exists():
        raise FileNotFoundError(in_path)
    if not mapping_path.exists():
        raise FileNotFoundError(mapping_path)

    if project_name is None:
        project_name = in_path.stem

    pf_utils.delete_project_if_exists(app, project_name)
    pf_utils.import_pfd_into_current_user(app, in_path)
    pf_utils.activate_project(app, project_name)

    restore_from_mapping(app, mapping_path)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    pf_utils.export_project_to_pfd(app, out_path)
    logger.info("=== restore_PF.py: End Reverse ===")
