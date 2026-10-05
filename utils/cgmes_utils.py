"""
cgmes_utils.py
===============

Helper utilities for reading, anonymizing, and repacking CGMES
(Common Grid Model Exchange Standard) XML bundles.

This module bundles the functionality needed to process CGMES files
end-to-end:

- RDF/XML tag helpers for working with Clark-notation tags and
  rdf:ID / rdf:about / rdf:resource attributes (`local`, `remap_id`,
  `strip_hash`, `get_parent_rdfinfo`).
- Lookup sets that classify which elements carry anonymization-relevant
  free text, GPS coordinates, timestamps, or line specification data
  (`ANON_TEXT_LOCALS`, `GPS_X_LOCALS`, `GPS_Y_LOCALS`,
  `TIME_STAMP_LOCALS`, `LINE_SPECS_LOCALS`).
- Conversion between CGMES timestamp strings and Unix epoch seconds
  (`cgmes_time_to_epoch`, `epoch_to_cgmes_time`).
- XML parsing/serialisation via lxml (`parse_xml`, `serialise_xml`).
- Extracting a CGMES bundle (zip, directory, or single XML file) into
  a working directory and packing processed files back up
  (`extract_bundle`, `pack_bundle`).
"""

# pylint: disable=c-extension-no-member
import shutil
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Set, Tuple

from lxml import etree

# ---------------------------------------------------------------------------
# RDF namespace
# ---------------------------------------------------------------------------
RDF_NS = "http://www.w3.org/1999/02/22-rdf-syntax-ns#"
RDF_ID = f"{{{RDF_NS}}}ID"
RDF_ABOUT = f"{{{RDF_NS}}}about"
RDF_RESOURCE = f"{{{RDF_NS}}}resource"

# ---------------------------------------------------------------------------
# Local-name suffixes of text elements whose content gets anonymized.
# Matching is done on the local part after the last '}' in the Clark tag.
# ---------------------------------------------------------------------------
ANON_TEXT_LOCALS: Set[str] = {
    "IdentifiedObject.name",
    "IdentifiedObject.description",
    "IdentifiedObject.shortName",
    "IdentifiedObject.aliasName",
    "Model.description",  # md:FullModel header – often contains TSO/DSO name
}

# GPS coordinate element local names (GL profile, CGMES 2.4 and 3.0)
GPS_X_LOCALS: Set[str] = {
    "PositionPoint.xPosition",  # longitude in CGMES GL
    "CoordinatePair.xPosition",  # older profile variant
}
GPS_Y_LOCALS: Set[str] = {
    "PositionPoint.yPosition",  # latitude in CGMES GL
    "CoordinatePair.yPosition",
}

TIME_STAMP_LOCALS: Set[str] = {"Model.scenarioTime"}

LINE_SPECS_LOCALS: Set[str] = {
    "Conductor.length",
    "ACLineSegment.b0ch",
    "ACLineSegment.bch",
    "ACLineSegment.g0ch",
    "ACLineSegment.gch",
    "ACLineSegment.r",
    "ACLineSegment.r0",
    "ACLineSegment.x",
    "ACLineSegment.x0",
}

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def local(tag: str) -> str:
    """
    Strip the namespace off a Clark-notation XML tag.
    """
    return tag.split("}")[-1] if "}" in tag else tag


def strip_hash(ref: str) -> str:
    """
    Remove a leading '#' from an rdf:resource / rdf:about value.
    """
    return ref[1:] if ref.startswith("#") else ref


def get_parent_rdfinfo(element: etree._Element, rdf_tag: str) -> str | None:
    """
    Read an RDF identifier attribute from an element's parent.

    Parameters
    ----------
    element : lxml.etree._Element
        The child element whose parent should be inspected.
    rdf_tag : str
        Clark-notation attribute name to read (e.g. `RDF_ID`).

    Returns
    -------
    str or None
        The attribute value on the parent element, or None if unset.
    """
    parent = element.getparent()
    return parent.get(rdf_tag)


# ---------- Courtesy of Claude ------------
def cgmes_time_to_epoch(timestr: str) -> int:
    """
    Convert a CGMES timestamp string to Unix epoch seconds.

    Parameters
    ----------
    timestr : str
        Timestamp in CGMES format, e.g. "1977-01-01T09:00:00Z".

    Returns
    -------
    int
        Seconds since 1970-01-01 (UTC).
    """
    dt = datetime.strptime(timestr, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    return int(dt.timestamp())


def epoch_to_cgmes_time(epoch: int) -> str:
    """
    Convert Unix epoch seconds to a CGMES timestamp string.

    Parameters
    ----------
    epoch : int
        Seconds since 1970-01-01 (UTC).

    Returns
    -------
    str
        Timestamp in CGMES format, e.g. "1977-01-01T09:00:00Z".
    """
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ------------------------------------------

# ---------------------------------------------------------------------------
# XML I/O
# ---------------------------------------------------------------------------


def parse_xml(path: Path) -> etree._ElementTree:
    """
    Parse an XML file into an lxml element tree, preserving formatting.

    Parameters
    ----------
    path : Path
        Path to the XML file to parse.

    Returns
    -------
    etree._ElementTree
        The parsed element tree.
    """
    parser = etree.XMLParser(remove_comments=False, remove_blank_text=False)
    return etree.parse(str(path), parser)


def serialise_xml(tree: etree._ElementTree, path: Path) -> None:
    """
    Write an lxml element tree to disk as a pretty-printed XML file.

    Parameters
    ----------
    tree : etree._ElementTree
        The element tree to serialise.
    path : Path
        Destination file path.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tree.write(
        str(path),
        xml_declaration=True,
        encoding="utf-8",
        pretty_print=True,
    )


# ---------------------------------------------------------------------------
# Bundle I/O helpers
# ---------------------------------------------------------------------------


def extract_bundle(src: Path, tmp_dir: Path) -> List[Tuple[str, Path]]:
    """
    Extract a CGMES bundle into a working directory.

    Parameters
    ----------
    src : Path
        Source bundle: a directory, a .zip file, or a single .xml file.
    tmp_dir : Path
        Working directory the bundle is extracted/copied into.

    Returns
    -------
    List[Tuple[str, Path]]
        (relative_name, absolute_path) pairs for every extracted .xml
        file, with `relative_name` relative to `tmp_dir`.
    """
    xml_files: List[Tuple[str, Path]] = []

    if src.is_dir():
        for f in sorted(src.rglob("*.xml")):
            rel = str(f.relative_to(src))
            dest = tmp_dir / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(f, dest)
            xml_files.append((rel, dest))

    elif zipfile.is_zipfile(src):
        with zipfile.ZipFile(src, "r") as zf:
            zf.extractall(tmp_dir)
        for f in sorted(tmp_dir.rglob("*.xml")):
            rel = str(f.relative_to(tmp_dir))
            xml_files.append((rel, f))

    else:
        # Treat as a single XML file
        dest = tmp_dir / src.name
        shutil.copy2(src, dest)
        xml_files.append((src.name, dest))

    return xml_files


def pack_bundle(
    xml_files: List[Tuple[str, Path]],
    out: Path,
) -> None:
    """
    Pack processed XML files back into a zip archive or an output directory.

    Parameters
    ----------
    xml_files : List[Tuple[str, Path]]
        (relative_name, absolute_path) pairs, as returned by
        `extract_bundle`, identifying the files to pack and the
        relative path each should have in the output.
    out : Path
        Destination path: a `.zip` file, or a directory.
    """
    if out.suffix.lower() == ".zip":
        out.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for rel, path in xml_files:
                zf.write(path, rel)
    else:
        out.mkdir(parents=True, exist_ok=True)
        for rel, path in xml_files:
            dest = out / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, dest)
