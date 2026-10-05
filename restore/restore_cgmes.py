"""
restore_cgmes.py
=================

Reverses a previously anonymized CGMES bundle back to its original
values, using the mapping JSON produced during anonymization.

Given an anonymized CGMES bundle (zip, directory, or single XML) and
the corresponding mapping file, this module extracts the bundle,
walks every element in every XML tree, and restores free text
(`_restore_textfields`), rdf:IDs (`_restore_rdfids`), GPS coordinates
(`_restore_gps`), line specification values (`_restore_line_length`),
and timestamps (`_restore_time`), before repacking the tree into the
output bundle via the public `restore_cgmes` entrypoint.
"""

# pylint: disable=c-extension-no-member
import logging
import tempfile
from pathlib import Path
from typing import Dict, List, Tuple

from lxml import etree

from utils import cgmes_utils, utils

logger = logging.getLogger("restore_cgmes.py")

# ---------------------------------------------------------------------------
# Per-tree restore pass
# ---------------------------------------------------------------------------


def _restore_tree(
    tree: etree._ElementTree,
    *,
    anon_rev: Dict[str, str],
    cim_rev: Dict[str, str],
    time_rev: Dict[str, str],
    gps_map: Dict[str, dict],
    prefix: str,
    line_map: Dict[str, str],
) -> None:
    """Reverse anonymization in-place for each file."""

    _restore_textfields(tree, anon_rev=anon_rev, prefix=prefix)
    _restore_rdfids(tree, cim_rev=cim_rev)
    _restore_gps(tree, gps_map=gps_map)
    _restore_line_length(tree, line_map=line_map)
    _restore_time(tree, time_rev=time_rev)


def _restore_textfields(
    tree: etree._ElementTree,
    *,
    anon_rev: Dict[str, str],
    prefix: str,
):
    """
    If the element applies to the text locals, set it to the old values
    """
    for el in tree.iter():
        loc = cgmes_utils.local(el.tag)
        if loc in cgmes_utils.ANON_TEXT_LOCALS and el.text:
            cur = el.text.strip()
            if cur.startswith(prefix) and cur in anon_rev:
                el.text = anon_rev[cur]


def _restore_rdfids(
    tree: etree._ElementTree,
    *,
    cim_rev: Dict[str, str],
):
    """
    get the cim rdf ids of the element and set it to the old value
    """
    if cim_rev:
        for el in tree.iter():
            raw_id = el.get(cgmes_utils.RDF_ID)
            if raw_id and raw_id in cim_rev:
                el.set(cgmes_utils.RDF_ID, cim_rev[raw_id])

            raw_about = el.get(cgmes_utils.RDF_ABOUT)
            if raw_about:
                bare = cgmes_utils.strip_hash(raw_about)
                if bare in cim_rev:
                    orig = cim_rev[bare]
                    el.set(
                        cgmes_utils.RDF_ABOUT,
                        "#" + orig if raw_about.startswith("#") else orig,
                    )

            raw_res = el.get(cgmes_utils.RDF_RESOURCE)
            if raw_res:
                bare = cgmes_utils.strip_hash(raw_res)
                if bare in cim_rev:
                    orig = cim_rev[bare]
                    el.set(
                        cgmes_utils.RDF_RESOURCE,
                        "#" + orig if raw_res.startswith("#") else orig,
                    )


def _restore_gps(
    tree: etree._ElementTree,
    *,
    gps_map: Dict[str, dict],
):
    """
    Check tree if elements applies to GPS Data, and set it to the old data
    """
    gps_buckets: Dict[str, Dict[str, etree._Element]] = {}

    for el in tree.iter():
        loc = cgmes_utils.local(el.tag)
        if loc not in cgmes_utils.GPS_X_LOCALS and loc not in cgmes_utils.GPS_Y_LOCALS:
            continue

        # first get which net element the gps data applies to
        parent = el.getparent()
        if parent is None:
            continue

        key = parent.get(cgmes_utils.RDF_ID) or cgmes_utils.strip_hash(
            parent.get(cgmes_utils.RDF_ABOUT) or ""
        )

        # put the element in the gps bucket
        bucket = gps_buckets.setdefault(key, {})
        if loc in cgmes_utils.GPS_X_LOCALS:
            bucket["x_el"] = el
        else:
            bucket["y_el"] = el

    # iter through the  gps bucket and set the coordinates back
    for parent_id, bucket in gps_buckets.items():
        rec = gps_map.get(parent_id)
        if rec is None:
            continue
        old = rec.get("old")
        if not (isinstance(old, list) and len(old) == 2):
            continue
        old_lat, old_lon = float(old[0]), float(old[1])
        x_el = bucket.get("x_el")
        y_el = bucket.get("y_el")
        if x_el is not None:
            x_el.text = f"{old_lon}"
        if y_el is not None:
            y_el.text = f"{old_lat}"


def _restore_line_length(
    tree: etree._ElementTree,
    *,
    line_map: Dict[str, str],
):
    """
    Check tree if elements applies to Line Specs, and set it to the old
    line specs
    """
    for el in tree.iter():
        loc = cgmes_utils.local(el.tag)
        if loc not in cgmes_utils.LINE_SPECS_LOCALS:
            continue
        # getting the length of the element
        try:
            rdf_id = cgmes_utils.get_parent_rdfinfo(el, cgmes_utils.RDF_ID)
            orig_value = float(line_map[rdf_id][loc])
        except KeyError:
            if el.base.endswith("SC_.xml"):
                continue
        el.text = utils.format_float(orig_value)


def _restore_time(
    tree: etree._ElementTree,
    *,
    time_rev: Dict[str, str],
):
    """
    Check tree if elements applies to Time Stamps, and set it to the old
    times
    """
    for el in tree.iter():
        loc = cgmes_utils.local(el.tag)
        if loc in cgmes_utils.TIME_STAMP_LOCALS:
            anon_time_cgmes = el.text.strip()
            anon_time_epoch = cgmes_utils.cgmes_time_to_epoch(anon_time_cgmes)
            orig_time_epoch = int(time_rev[str(anon_time_epoch)])
            el.text = cgmes_utils.epoch_to_cgmes_time(orig_time_epoch)


# ---------------------------------------------------------------------------
# Public entrypoint: restore
# ---------------------------------------------------------------------------


def restore_cgmes(
    in_path: Path,
    out_path: Path,
    mapping_path: Path,
) -> None:
    """
    Reverse a previously anonymized CGMES bundle using the mapping JSON.

    Parameters
    ----------
    in_path      : anonymized zip / folder / XML
    out_path     : restored output
    mapping_path : mapping JSON produced by anonymize_cgmes
    """
    in_path = Path(in_path)
    out_path = Path(out_path)
    mapping_path = Path(mapping_path)

    logger.info("=== anym_cgmes.py: Start Restore ===")

    line_map, anon_rev, time_rev, cim_rev, __, gps_map, prefix = utils.get_mappings(
        mapping_path
    )

    with tempfile.TemporaryDirectory() as tmp_str:
        tmp_dir = Path(tmp_str)
        xml_files = cgmes_utils.extract_bundle(in_path, tmp_dir)
        logger.info(
            "  Found %d XML file(s): %s", len(xml_files), [r for r, _ in xml_files]
        )

        trees: List[Tuple[str, Path, etree._ElementTree]] = []
        for rel, path in xml_files:
            try:
                trees.append((rel, path, cgmes_utils.parse_xml(path)))
            except etree.XMLSyntaxError as exc:
                logger.warning("  Skipping %s: %s", rel, exc)

        for rel, path, tree in trees:
            logger.info("  Restoring %s ...", rel)
            _restore_tree(
                tree,
                anon_rev=anon_rev,
                cim_rev=cim_rev,
                time_rev=time_rev,
                gps_map=gps_map,
                prefix=prefix,
                line_map=line_map,
            )
            cgmes_utils.serialise_xml(tree, path)

        out_path.parent.mkdir(parents=True, exist_ok=True)
        cgmes_utils.pack_bundle([(rel, path) for rel, path, _ in trees], out_path)

    logger.info("  Output: %s", out_path)
    logger.info("=== anym_cgmes.py: Restore Done ===")
