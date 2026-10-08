"""
pf_utils.py
===========

Helper utilities for automating DIgSILENT PowerFactory via its Python
API.

This module bundles the functionality needed to script PowerFactory
end-to-end:

- Locating a compatible PowerFactory installation and importing the
  `powerfactory` module (`get_pf_version`, `check_python_pf_compatibility`,
  `import_powerfactory_module`).
- Thin wrappers around PF calls that may live on either the `pf` module
  or the application object, plus bulk-mode helpers to speed up large
  edits (`_call_pf_or_app`, `pf_bulk_mode_begin`, `pf_bulk_mode_end`).
- Collecting all network-relevant PF objects in a project, including
  their parent chains, as a basis for anonymization (`PfObjects`,
  `parent_chain_until_network_data`, `collect_unique_objects_for_anonymization`).
- Safe getters/setters for PF attributes that tolerate objects which
  don't support a given attribute (`get_float_attr`, `safe_set`,
  `get_loc_name`, `get_str_attr`, `set_str_attr`, `get_cim_rdf_id`,
  `set_cim_rdf_id`, `get_full_name`).
- Renaming objects uniquely and sanitizing/anonymizing free-text
  description fields (`make_unique_if_needed`, `sanitize_desc` and the
  related `_desc_*` helpers).
- Project import/activate/export workflows and process management
  (`delete_project_if_exists`, `import_pfd_into_current_user`,
  `activate_project`, `export_project_to_pfd`, `kill_powerfactory`).

Requires a local PowerFactory installation; if none is found,
`pf` is set to `None` and PF-dependent functions will fail when called.
"""

import logging
import os
import re
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import psutil

logger = logging.getLogger("pf_utils.py")

IMPEDANCE_TYPES = [
    "rline",
    "xline",
    "cline",
    "lline",
    "rline0",
    "xline0",
    "cline0",
    "lline0",
]  # do the 0 impedances actually need to be reset?


# ----------------------------
# power factory version check
# ----------------------------
def get_pf_version() -> Path:
    """
    Locate the newest installed PowerFactory version.

    Returns
    -------
    Path
        Install directory of the most recent PowerFactory version.
    """
    # Getting the PowerFactory Version
    search_paths = [
        Path(r"C:\Program Files\DIgSILENT"),
        Path(r"C:\Program Files (x86)\DIgSILENT"),
    ]

    versions = {}

    for base in search_paths:
        if not base.exists():
            continue

        for entry in base.iterdir():
            if entry.is_dir() and entry.name.startswith("PowerFactory"):
                version = entry.name.replace("PowerFactory", "").strip()

                versions[version] = str(entry)

    versions = {
        version: path
        for version, path in versions.items()
        if "LicenceManager".lower() not in version.lower()
    }
    if not versions:
        return False
    else:
        _, last_path = sorted(versions.items())[-1]
        return Path(last_path)


def check_python_pf_compatibility(powerfactory_path: Path, py_version: str) -> None:
    """
    Verify that the running Python's major.minor version is supported
    by the detected PowerFactory installation.
    """
    search_path = Path(powerfactory_path, "Python")
    possible_versions = [version.name for version in search_path.iterdir()]
    if not any(version == py_version for version in possible_versions):
        raise RuntimeError(
            f"""\nError: This Python Version {py_version} is not compatible with the current 
            version of PowerFactory. Try one of the following Python versions instead: 
            {possible_versions}.\n"""
        )


def import_powerfactory_module():
    """
    Import and return the `powerfactory` module for the detected install.

    Returns
    -------
    module or None
        The imported `powerfactory` module, or None if no PowerFactory
        installation was found in the standard locations.
    """
    pf_path = get_pf_version()
    if pf_path is False:
        pf_module = None  # pylint:disable=invalid-name
        logger.warning("No PowerFactory installation found in standard locations.")
    else:
        # set python version
        python_major_version = sys.version_info.major
        python_minor_version = sys.version_info.minor
        python_version = f"{str(python_major_version)}.{str(python_minor_version)}"

        check_python_pf_compatibility(pf_path, python_version)

        # PowerFactory Python path
        pf_python_path = Path(pf_path, "Python", python_version)

        sys.path.append(str(pf_python_path))

        import powerfactory as pf_module  # type: ignore # pylint: disable=import-error,wrong-import-position,wrong-import-order, import-outside-toplevel

    return pf_module


def get_p(p: Path) -> str:
    """
    wrapper to get the pathlib object p to a string format
    """
    return os.fspath(Path(p).resolve())


pf = import_powerfactory_module()


# ----------------------------
# PF call wrappers
# ----------------------------
def _call_pf_or_app(app, name: str, *args):
    """
    Call a method by name on whichever of `pf` or `app` defines it.

    PowerFactory exposes some functions on the `powerfactory` module
    itself and others on the application object, depending on version;
    this abstracts over that difference.

    Raises
    ------
    AttributeError
        If neither `pf` nor `app` defines `name`.
    """

    if hasattr(pf, name):
        return getattr(pf, name)(*args)
    if hasattr(app, name):
        return getattr(app, name)(*args)
    raise AttributeError(f"Neither pf nor app have: {name}")


def pf_bulk_mode_begin(app):
    """
    Switch PowerFactory into bulk-editing mode.

    Disables progress bar updates, GUI updates, and user break
    handling, and enables the write cache, so that large numbers of
    scripted changes run faster. Should be paired with a matching
    call to `pf_bulk_mode_end`.

    Parameters
    ----------
    app : the PowerFactory application object (from pf.GetApplication()).
    """
    _call_pf_or_app(app, "SetProgressBarUpdatesEnabled", 1)
    _call_pf_or_app(app, "SetGuiUpdateEnabled", 1)
    _call_pf_or_app(app, "SetUserBreakEnabled", 1)
    _call_pf_or_app(app, "SetWriteCacheEnabled", 1)


def pf_bulk_mode_end(app):
    """
    Leave bulk-editing mode and flush pending changes to the database.

    Writes cached changes to the database, then re-enables user break
    handling, GUI updates, and progress bar updates, undoing the
    effects of `pf_bulk_mode_begin`.

    Parameters
    ----------
    app : the PowerFactory application object (from pf.GetApplication()).
    """
    _call_pf_or_app(app, "WriteChangesToDb")
    _call_pf_or_app(app, "SetWriteCacheEnabled", 0)
    _call_pf_or_app(app, "SetUserBreakEnabled", 0)
    _call_pf_or_app(app, "SetGuiUpdateEnabled", 0)
    _call_pf_or_app(app, "SetProgressBarUpdatesEnabled", 0)


# ----------------------------
# PF object collection
# ----------------------------
class PfObjects:
    """
    Collects all network-relevant PowerFactory objects for a project.

    On construction, gathers every object matching a fixed set of class
    patterns (elements, types, switches, cubicles, graphics, project
    folders), deletes any CimModel objects found in the active project,
    and clears (renames to "Deleted") any IntGrf map info objects, since
    these typically carry no anonymization-relevant data but may leak
    project metadata.
    """

    def __init__(self, app):
        """
        Build the object collection for the given PowerFactory application.

        Parameters
        ----------
        app : the PowerFactory application object (from pf.GetApplication()).
        """
        patterns = [
            "*.IntPrjfolder",
            "*.IntQlim",
            "*.Elm*",
            "*.ElmLod",
            "*.Typ*",
            "*.StaSwitch",
            "*.StaCubic",
        ]

        # add all calculation relevant objects
        self.objects = []
        for pat in patterns:
            try:
                self.objects += app.GetCalcRelevantObjects(pat) or []
            except (AttributeError, TypeError):
                pass

        # delete cim models for anonymization
        project = app.GetActiveProject()
        cim_models = project.GetContents("*.CimMdel", 1)
        for cim_model in cim_models:
            try:
                cim_model.Delete()
            except AttributeError:
                pass

        # add certain objects, that are not relevant to calculations e.g. graphics names to objects
        patterns = [
            "*.IntGrfnet",
            "*.IntEvt",
            "*.IntPlannedout",
            "*.EvtShc",
            "*.IntCase",
            "*.SetPrj",
        ]
        for pat in patterns:
            new_objs = project.GetContents(pat, 1)
            try:
                self.objects += new_objs or []
            except (AttributeError, TypeError) as e:
                logger.error("Error adding %s to objects: %s", pat, e)

        mapsinfos = project.GetContents("*.IntGrf", 1)

        # delete names of graphical elements
        for single_map in mapsinfos:
            try:
                # map.Delete()
                single_map.loc_name = "Deleted"
            except AttributeError:
                pass

    def iter_all_lists(self):
        """Yield every collected PF object."""
        yield from self.objects


def parent_chain_until_network_data(obj) -> List:
    """
    Walk up an object's parent chain, stopping after "Network Data".

    Returns
    -------
    List
        The object plus all ancestors, starting with `obj` itself and
        ending at the first ancestor named "Network Data" (inclusive),
        or at the root if "Network Data" is never reached.
    """
    chain = []
    current = obj
    while current:
        chain.append(current)
        if getattr(current, "loc_name", None) == "Network Data":
            break
        current = current.GetParent()
    return chain


def collect_unique_objects_for_anonymization(app) -> List:
    """
    Build a deduplicated list of all objects to anonymize, including
    their parent chains.

    Returns
    -------
    List
        Unique PF objects (original objects plus their relevant ancestors).
    """
    pf_objs = PfObjects(app)
    unique: Dict[str, object] = {}

    for obj in pf_objs.iter_all_lists():
        try:
            key = obj.GetFullName()
        except AttributeError:
            key = f"{obj.GetClassName()}::{getattr(obj, 'loc_name', '')}"
        unique.setdefault(key, obj)

        for parent in parent_chain_until_network_data(obj):
            try:
                pkey = parent.GetFullName()
            except AttributeError:
                pkey = f"{parent.GetClassName()}::{getattr(parent, 'loc_name', '')}"
            unique.setdefault(pkey, parent)

    return list(unique.values())


# ----------------------------
# Safe attribute helpers
# ----------------------------
def get_float_attr(obj, attr: str) -> Optional[float]:
    """
    Safely read a PF attribute as a float.

    Returns None if the attribute doesn't exist, is unset, or can't be
    converted to a float (instead of raising).
    """
    try:
        if not obj.HasAttribute(attr):
            return None
    except AttributeError:
        return None

    try:
        v = obj.GetAttribute(attr)
        if v is None:
            return None
        return float(v)
    except AttributeError:
        try:
            return float(getattr(obj, attr))
        except AttributeError:
            return None


def safe_set(obj, attr, value, *, verbose: bool = False) -> bool:
    """
    Safely set a PF attribute, tolerating objects that don't support it.

    Returns
    -------
    bool
        True if the attribute was successfully set, False otherwise.
    """
    try:
        if not obj.HasAttribute(attr):
            return False
    except AttributeError as e:
        if verbose:
            logger.warning("HasAttribute(%s) failed: %s", attr, e)
        return False

    try:
        obj.SetAttribute(attr, value)
        return True
    except TypeError as e:
        if isinstance(value, str):
            try:
                obj.SetAttribute(attr, [value])
                return True
            except TypeError:
                pass
        if verbose:
            logger.warning(
                "TypeError SetAttribute(%s) on %s (%s): %s",
                attr,
                obj.GetClassName(),
                getattr(obj, "loc_name", ""),
                e,
            )
        return False
    except AttributeError as e:
        if verbose:
            logger.warning(
                "SetAttribute(%s) failed on %s (%s): %s",
                attr,
                obj.GetClassName(),
                getattr(obj, "loc_name", ""),
                e,
            )
        return False


def get_loc_name(obj) -> str:
    """
    Safely read an object's `loc_name` attribute.

    Returns
    -------
    str
        The object's local name, or "" if it cannot be determined.
    """
    try:
        return obj.GetAttribute("loc_name")
    except AttributeError:
        return getattr(obj, "loc_name", "")


def _set_loc_name_only(obj, new_name: str):
    """Set an object's `loc_name` attribute directly, without uniqueness checks."""
    try:
        return obj.SetAttribute("loc_name", new_name)
    except AttributeError:
        return setattr(obj, "loc_name", new_name)


def get_str_attr(obj, attr: str) -> Optional[str]:
    """
    Safely read a PF attribute as a string.

    Returns
    -------
    Optional[str]
        The attribute value as a string, "" if unset, or None if the
        attribute is unavailable.
    """
    try:
        if not obj.HasAttribute(attr):
            return None
    except AttributeError:
        return None

    try:
        v = obj.GetAttribute(attr)
    except AttributeError:
        try:
            v = getattr(obj, attr)
        except AttributeError:
            return None

    if v is None:
        return ""

    # PF attributes may be list-like
    if isinstance(v, (list, tuple)):
        if len(v) == 0:
            return ""
        v0 = v[0]
        return "" if v0 is None else str(v0)

    return str(v)


def set_str_attr(obj, attr: str, value: str) -> bool:
    """
    Safely set a PF attribute to the string form of `value`.

    Returns
    -------
    bool
        True if the attribute was successfully set, False otherwise.
    """
    return safe_set(obj, attr, str(value), verbose=False)


def get_cim_rdf_id(obj) -> List[str]:
    """
    Safely read an object's `cimRdfId` attribute.

    Returns
    -------
    List[str]
        The object's CIM RDF ID(s), or an empty list if unavailable.
    """
    try:
        if not obj.HasAttribute("cimRdfId"):
            return []
    except AttributeError:
        return []
    try:
        value = obj.GetAttribute("cimRdfId")
        return value or []
    except AttributeError:
        return []


def set_cim_rdf_id(obj, new_id: str) -> bool:
    """
    Safely set an object's `cimRdfId` attribute to a single-element list.

    Returns
    -------
    bool
        True if the attribute was successfully set, False otherwise.
    """
    return safe_set(obj, "cimRdfId", [new_id], verbose=False)


def get_full_name(obj) -> str:
    """
    Safely read an object's fully qualified PF name.

    Returns
    -------
    str
        The object's full PF path, or a class/name fallback string.
    """
    try:
        return obj.GetFullName()
    except AttributeError:
        return f"{obj.GetClassName()}::{get_loc_name(obj)}"


def set_project_unit(obj, desired_unit_system=0, desired_unit="k"):

    unit_system = get_str_attr(obj, "ilenunit")
    current_unit = get_str_attr(obj, "clenexp")

    if unit_system != desired_unit_system:
        set_str_attr(obj, "ilenunit", desired_unit_system)
    if current_unit != desired_unit and desired_unit_system == 0:
        set_str_attr(obj, "clenexp", desired_unit)

    return unit_system, current_unit


def _to_project_relative(full_name: str) -> str:
    """Trim a full PF object path down to the part relative to the project."""
    marker = r"\Network Model.IntPrjfolder"
    i = full_name.find(marker)
    if i < 0:
        return full_name
    return full_name[i:]


def search_by_full_name_after(app, full_name_after: str):
    """
    Find an object in the active project by its (post-move) full name.

    Returns
    -------
    The matching PF object, or None if there is no active project or
    the object cannot be found.
    """
    project = app.GetActiveProject()
    if not project:
        return None

    rel = _to_project_relative(full_name_after)
    try:
        return project.SearchObject(rel)
    except AttributeError:
        return None


def make_unique_if_needed(obj, desired: str, anonymizer) -> str:
    """
    Rename `obj` to `desired`, disambiguating with a hash suffix if needed.

    Parameters
    ----------
    obj : the PF object to rename.
    desired : str
        The name to try to apply.
    anonymizer : utils.SeededNameAnonymizer
        Used to derive a deterministic hash suffix when a plain rename
        is not possible.

    Returns
    -------
    str or None
        The name that was actually applied (either `desired` or a
        `desired_<hash>` candidate), or None if the object was skipped
        because it's in the exception list or is a project folder.
    """
    old = get_loc_name(obj)
    full = get_full_name(obj)
    exception_list = [
        "IntArea",
        "IntBmu",
        "IntBoundary",
        "IntBbone",
        "IntCircuit",
        "IntDependency",
        "IntFeeders",
        "IntLvscale",
        "IntOperator",
        "IntOwner",
        "IntStyle",
        "IntPath",
        "IntRoute",
        "IntZone",
        "SetFold",
        "Fault.IntCase",
    ]
    if full.endswith(".IntPrjfolder"):
        return
    if full.endswith(tuple(exception_list)):
        return
    try:
        _set_loc_name_only(obj, desired)
        if get_loc_name(obj) == desired:
            return desired
        raise RuntimeError("PF did not apply loc_name")
    except AttributeError:
        try:
            base = get_full_name(obj)
        except AttributeError:
            base = f"{obj.GetClassName()}::{old}"
        suffix = anonymizer.get_hash(base, 6)
        candidate = f"{desired}_{suffix}"
        _set_loc_name_only(obj, candidate)
        if get_loc_name(obj) != candidate:
            logger.warning(
                "Rename failed: %s -> %s (candidate %s not applied)",
                old,
                desired,
                candidate,
            )
        return candidate


def build_cim_index(objs: List) -> Dict[str, object]:
    """
    Build a lookup table from CIM RDF ID to PF object.

    Parameters
    ----------
    objs : List
        PF objects to index.

    Returns
    -------
    Dict[str, object]
        Mapping from CIM RDF ID to the corresponding PF object.
    """
    idx: Dict[str, object] = {}
    for o in objs:
        ids = get_cim_rdf_id(o)
        if ids:
            idx[ids[0]] = o
    return idx


def _collapse_semicolons(s: str) -> str:
    """
    Collapse runs of semicolons into a single one and strip leading/trailing ones.
    """
    s = re.sub(r";{2,}", ";", s)  # ;; oder mehr -> ;
    s = s.strip(";")
    return s


# ----------------------------
# DESC handling
# ----------------------------
def sanitize_desc(obj, desc: bool, anonymizer):
    """
    desc=True  -> delete description (write 'Deleted')
    desc=False -> anonymize description (token-based, reversible via anon_mapping)
    """
    if desc:
        safe_set(obj, "desc", "Deleted", verbose=False)
        return

    old = get_str_attr(obj, "desc")
    if old is None:
        return

    old_s = str(old)
    if old_s.strip() == "":
        return

    new_s = _desc_anonymize(old_s, anonymizer)
    if new_s != old_s:
        safe_set(obj, "desc", new_s, verbose=False)


def desc_normalize(s: str) -> str:
    """Normalize a description string for tokenization."""
    if s is None:
        return ""

    t = str(s)

    out = []
    prev_space = False
    for ch in t:
        if ch.isspace():
            if not prev_space:
                out.append(" ")
            prev_space = True
        else:
            out.append(ch)
            prev_space = False

    return "".join(out)


def _desc_tokenize_keep_delims(s: str) -> List[Tuple[str, bool]]:
    """Tokenize a description string, keeping ";" and " " as delimiter items."""
    s = desc_normalize(s)

    items: List[Tuple[str, bool]] = []
    buf: List[str] = []

    def flush_token():
        nonlocal buf
        if buf:
            tok = "".join(buf)
            if tok != "":
                items.append((tok, False))
            buf = []

    for ch in s:
        if ch == ";":
            flush_token()
            items.append((";", True))
        elif ch == " ":
            flush_token()
            items.append((" ", True))
        else:
            buf.append(ch)

    flush_token()

    while items and items[0] == (" ", True):
        items.pop(0)
    while items and items[-1] == (" ", True):
        items.pop()

    return items


def _desc_anonymize(desc_value: str, anonymizer) -> str:
    """Anonymize each word token in a description, joining tokens with ";"."""
    seq = _desc_tokenize_keep_delims(desc_value)
    if not seq:
        return desc_value if desc_value is not None else ""

    out_parts: List[str] = []
    for text, is_delim in seq:
        if is_delim:
            out_parts.append(text)
        else:
            tok = text.strip()
            if tok == "":
                continue
            out_parts.append(anonymizer.translate(tok))

    out = desc_normalize("".join(out_parts)).strip()
    out = out.replace(" ", ";")
    while ";;" in out:
        out = out.replace(";;", ";")
    out = out.strip(";")

    return out


def _desc_restore(desc_value: str, anon_rev: Dict[str, str], prefix: str) -> str:

    seq = _desc_tokenize_keep_delims(desc_value)
    if not seq:
        return desc_value if desc_value is not None else ""

    out_parts: List[str] = []
    for text, is_delim in seq:
        if is_delim:
            out_parts.append(text)
        else:
            tok = text.strip()
            if tok == "":
                continue
            if tok.startswith(prefix):
                out_parts.append(anon_rev.get(tok, tok))
            else:
                out_parts.append(tok)

    out = desc_normalize("".join(out_parts)).strip()
    out = _collapse_semicolons(out)
    return out


# ----------------------------
# Import / Activate / Export
# ----------------------------
def _list_projects(user):
    return user.GetContents("*.IntPrj") or []


def delete_project_if_exists(app, project_name: str):
    """
    Delete the project named `project_name`, if it exists, for the current user.

    Parameters
    ----------
    app : the PowerFactory application object (from pf.GetApplication()).
    project_name : str
        Local name of the project to delete.
    """
    user = app.GetCurrentUser()
    prjs = _list_projects(user)

    target = None
    for p in prjs:
        if getattr(p, "loc_name", "") == project_name:
            target = p
            break
    if not target:
        return

    active = app.GetActiveProject()
    if active and active == target:
        active.Deactivate()

    target.Delete()
    app.ClearRecycleBin()
    _call_pf_or_app(app, "WriteChangesToDb")


def import_pfd_into_current_user(app, in_path: Path):
    """
    Import a .pfd file into the current PF user's folder.

    Parameters
    ----------
    app : the PowerFactory application object (from pf.GetApplication()).
    in_path : Path
        Path to the .pfd file to import.

    Raises
    ------
    RuntimeError
        If the import returns a non-zero return code.
    """
    user = app.GetCurrentUser()

    import_obj = user.CreateObject("CompfdImport", "Import")
    import_obj.SetAttribute("e:g_file", get_p(in_path))
    import_obj.g_target = user

    rc = import_obj.Execute()
    import_obj.Delete()

    app.ClearRecycleBin()
    _call_pf_or_app(app, "WriteChangesToDb")

    if rc != 0:
        raise RuntimeError(f"PFD import failed. Return code: {rc}")


def activate_project(app, project_name: str):
    """
    Activate a project by name, with fallbacks for renamed anonymized copies.

    Parameters
    ----------
    app : the PowerFactory application object (from pf.GetApplication()).
    project_name : str
        Local name of the project to activate.

    Returns
    -------
    The now-active PF project object.

    Raises
    ------
    RuntimeError
        If the project cannot be activated by any of the above
        strategies.
    """
    rc = app.ActivateProject(project_name)
    if rc == 0:
        return app.GetActiveProject()

    if project_name.endswith("_anonym"):
        alt_name = project_name[:-7]  # remove "_anonym"
        rc2 = app.ActivateProject(alt_name)
        if rc2 == 0:
            logger.info("[INFO] Project name corrected to: %s", alt_name)
            return app.GetActiveProject()

    user = app.GetCurrentUser()
    prjs = _list_projects(user)

    for p in prjs:
        if getattr(p, "loc_name", "") in (
            project_name,
            project_name.replace("_anonym", ""),
        ):
            if hasattr(p, "Activate"):
                p.Activate()
                return app.GetActiveProject()

    logger.info("Available projects:")
    for p in prjs:
        try:
            logger.info(" - %s", p.loc_name)
        except AttributeError:
            pass

    raise RuntimeError(f"Could not activate project: {project_name} (rc={rc})")


def export_project_to_pfd(app, out_path: Path):
    """
    Export the active project to a .pfd file and delete it afterward.

    Parameters
    ----------
    app : the PowerFactory application object (from pf.GetApplication()).
    out_path : Path
        Destination path for the exported .pfd file.

    Raises
    ------
    RuntimeError
        If no ComPfdexport object can be obtained from the study case.
    """
    g_object = app.GetActiveProject()
    if g_object:
        g_object.Deactivate()

    pfd_export_obj = app.GetFromStudyCase("ComPfdexport")
    if not pfd_export_obj:
        raise RuntimeError("ComPfdexport not found (StudyCase).")

    pfd_export_obj.g_objects = [g_object]
    pfd_export_obj.g_file = get_p(out_path)

    pfd_export_obj.exportCurrentState = 1
    pfd_export_obj.g_undo = 0
    pfd_export_obj.exportModBye = 0
    pfd_export_obj.exportExternalFiles = 0
    pfd_export_obj.g_derivedFlat = 0
    pfd_export_obj.g_formerbuild = 0
    pfd_export_obj.g_targetbuild = ""

    pfd_export_obj.Execute()

    g_object.Delete()
    app.ClearRecycleBin()


# ----------------------------
# Process helper
# ----------------------------
def kill_powerfactory():
    """Terminate any running PowerFactory.exe process, if found."""
    for proc in psutil.process_iter(attrs=["pid", "name"]):
        try:
            if proc.info.get("name") and "PowerFactory" in proc.info["name"]:
                proc.kill()
                logger.info("PowerFactory terminated.")
                return
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            pass


def get_load_flow_results(
    app, path: Path, anon_rev: dict, prefix: str, project_name: str = None
):
    """Main Parts are taken from
    https://thesmartinsights.com/run-digsilent-powerfactory-via-the-python-api-jump-start-to-your-powerfactory-automatization/
    and adapted for this use case"""
    if project_name is None:
        project_name = path.stem

    delete_project_if_exists(app, project_name)
    import_pfd_into_current_user(app, path)
    activate_project(app, project_name)

    # get load flow object and execute
    ldf_object = app.GetFromStudyCase("ComLdf")  # get load flow object
    rc = ldf_object.Execute()  # execute load flow
    if rc != 0:
        return
    load_flow_results = {"generators": [], "lines": [], "busses": []}

    # get the generators and their active/reactive power and loading
    generators = app.GetCalcRelevantObjects("*.ElmSym")
    gen_dict: Dict[str, float] = {}

    for gen in generators:  # loop through list
        name = getattr(gen, "loc_name")  # get name of the generator

        if name.startswith(prefix):
            orig_name = anon_rev[name]
        else:
            orig_name = name

        try:
            genloading = getattr(gen, "c:loading")  # get loading
            gen_entry = {
                "loading": genloading,
            }

        except AttributeError:
            gen_entry = "Unknown"

        gen_dict[orig_name] = gen_entry

    load_flow_results["generators"] = gen_dict

    # get the lines and print their loading
    lines = app.GetCalcRelevantObjects("*.ElmLne")
    line_dict = {}
    for line in lines:  # loop through list
        name = getattr(line, "loc_name")  # get name of the line

        if name.startswith(prefix):
            orig_name = anon_rev[name]
        else:
            orig_name = name

        try:
            value = getattr(line, "c:loading")  # get value for the loading
            line_entry = {"loading": value}

        except AttributeError:
            line_entry = "Unknown"

        line_dict[orig_name] = line_entry
    load_flow_results["lines"] = line_dict

    # get the buses and print their voltage
    buses = app.GetCalcRelevantObjects("*.ElmTerm")
    bus_dict = {}
    for bus in buses:  # loop through list

        name = getattr(bus, "loc_name")  # get name of the bus

        if name.startswith(prefix):
            orig_name = anon_rev[name]
        else:
            orig_name = name

        try:
            amp = getattr(bus, "m:u1")  # get voltage magnitude
            phase = getattr(bus, "m:phiu")  # get voltage angle
            bus_entry = {"u": amp, "deg": phase}
            bus_dict[orig_name] = bus_entry
        except AttributeError:
            bus_entry = "Unknown"
            bus_dict[orig_name] = bus_entry

    load_flow_results["busses"] = bus_dict
    return load_flow_results
