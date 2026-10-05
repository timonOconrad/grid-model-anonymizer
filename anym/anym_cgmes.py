"""
anym_cgmes.py  –  CGMES (XML/RDF) anonymizer
==============================================
Anonymizes a CGMES bundle (zip archive, folder, or single XML file) using the
same seed-based SHA-256 approach and mapping JSON as anym_PF / anym_csv anym_json.

Design rationale
----------------
- rdf:ID / rdf:about / rdf:resource are UUIDs in conformant CGMES files.
  They carry no human-readable information and are therefore left untouched
  by default.  Use --remap-ids (API flag remap_ids=True) only if the source
  file encodes readable names inside the IDs (e.g. hand-crafted test models
  with IDs like "_SubStation_HamburgNord").

- IdentifiedObject.name / description / shortName / aliasName are
  always anonymized when present (these carry the actual clear-text names).

- md:Model.description in the FullModel header is anonymized (it often
  contains TSO/DSO names or project identifiers).

- GPS coordinates (PositionPoint / CoordinatePair x/yPosition) in the GL
  profile are transformed or deleted.

- Only fields that actually exist in the file are touched.

Depends on: lxml, utils.py, cgmes_utils.py (SeededNameAnonymizer etc.)
"""

# pylint: disable=c-extension-no-member
# from __future__ import annotations

import logging
import math
import tempfile
from pathlib import Path
from typing import Dict, List, Set, Tuple

from lxml import etree

from utils import cgmes_utils, utils

logger = logging.getLogger("anym_cgmes.py")


# ---------------------------------------------------------------------------
# GPS transform
# ---------------------------------------------------------------------------


def _apply_gps_pair(
    x_el: etree._Element,
    y_el: etree._Element,
    *,
    seed: str,
    gps_delete: bool,
    gps_transform,
    anonymizer: utils.SeededNameAnonymizer,
    parent_id: str,
) -> None:
    """
    Transform or delete a GPS coordinate pair.
    x = longitude, y = latitude (CGMES GL convention).
    Records the change in anonymizer.gps_mapping keyed by parent_id.

    Note: gps_transform is expected to be a pure translation (see
    build_geo_transform in utils.py).  The scale_back_to_valid_geo
    call below acts as a safety net for points very close to the poles.
    """
    try:
        old_lon = float((x_el.text or "").strip())
        old_lat = float((y_el.text or "").strip())
    except ValueError:
        return

    # Skip null islands (already zeroed)
    if abs(old_lat) < 1e-12 and abs(old_lon) < 1e-12:
        return

    if gps_delete:
        anonymizer.gps_mapping[parent_id] = {
            "old": [old_lat, old_lon],
            "deleted": True,
        }
        x_el.text = "0.0"
        y_el.text = "0.0"
        return

    # Global rotation + per-object jitter (up to 100 m)
    new_lat, new_lon = gps_transform(old_lat, old_lon)
    r_m = utils.get_hash_float(seed, f"|cgmes_gps_r|{parent_id}") * 100.0
    theta = 2.0 * math.pi * utils.get_hash_float(seed, f"|cgmes_gps_theta|{parent_id}")
    new_lat += utils.meters_to_deg_lat(r_m * math.sin(theta))
    new_lon += utils.meters_to_deg_lon(r_m * math.cos(theta), new_lat)

    # Scale back the shift vector if the result falls outside the valid
    # geographic range, preserving the shift direction.
    new_lat, new_lon = utils.scale_back_to_valid_geo(old_lat, old_lon, new_lat, new_lon)

    anonymizer.gps_mapping[parent_id] = {
        "old": [old_lat, old_lon],
        "new": [new_lat, new_lon],
    }
    x_el.text = f"{new_lon:.6f}"
    y_el.text = f"{new_lat:.6f}"


# ---------------------------------------------------------------------------
# Per-tree anonymization pass
# ---------------------------------------------------------------------------


def _anonymize_tree(
    tree: etree._ElementTree,
    *,
    seed: str,
    gps_delete: bool,
    gps_transform,
    anonymizer: utils.SeededNameAnonymizer,
    desc_delete: bool,
    remap_ids: bool,
) -> None:
    """
    Modifies the parsed XML tree in-place.

    Steps:
    1. Time: Add Random amount of time to the timestamps.
    2. GPS: collect x/y element pairs per parent, then transform/delete.
    3. Line Length: Set lengths of transmission lines to 1 km.
    4. Text fields: anonymize IdentifiedObject.name / description / etc.
    3. rdf:ID remapping (only if remap_ids=True).
    """

    # ------------------------------------------------------------------
    # Step 1: time_stamps
    # ------------------------------------------------------------------
    _anonymize_time(tree, anonymizer=anonymizer)

    # ------------------------------------------------------------------
    # Step 2: GPS – group x/y children by their parent element
    # ------------------------------------------------------------------
    # IMPORTANT: lxml creates a new Python proxy object on every call to
    # el.getparent(), so id(parent) is NOT stable across two calls for the
    # same underlying C node.  Two xPosition/yPosition siblings therefore
    # yield different id() values for the same XML parent → end up in
    # separate buckets → both incomplete → both skipped.
    #
    # Fix: use a deterministic string key derived from the parent element:
    #   1. rdf:ID attribute value  (present in most CGMES elements)
    #   2. stripped rdf:about value
    #   3. tree.getpath(parent)  – lxml XPath, unique & stable (e.g.
    #      '/rdf:RDF/cim:PositionPoint[3]')
    #
    #   gps_buckets : str_key -> {"x_el": element, "y_el": element}

    _anonymize_gps(
        tree=tree,
        seed=seed,
        gps_delete=gps_delete,
        gps_transform=gps_transform,
        anonymizer=anonymizer,
    )

    # ------------------------------------------------------------------
    # Step 3: line_length
    # ------------------------------------------------------------------
    _anonymize_line_specs(tree=tree, anonymizer=anonymizer)

    # ------------------------------------------------------------------
    # Step 4: text fields
    # ------------------------------------------------------------------
    _anonymize_text_fields(tree=tree, anonymizer=anonymizer, desc_delete=desc_delete)

    # ------------------------------------------------------------------
    # Step 5: rdf:ID remapping (optional, off by default)
    # ------------------------------------------------------------------
    _anonymize_rdf(
        tree=tree,
        seed=seed,
        anonymizer=anonymizer,
        remap_ids=remap_ids,
    )


def _anonymize_gps(
    tree: etree._ElementTree,
    *,
    seed: str,
    gps_delete: bool,
    gps_transform,
    anonymizer: utils.SeededNameAnonymizer,
):
    """
    Get all the GPS Data and apply the transformation.
    """

    def _parent_key(p: etree._Element) -> str:
        v = p.get(cgmes_utils.RDF_ID) or ""
        if v:
            return v
        v = cgmes_utils.strip_hash(p.get(cgmes_utils.RDF_ABOUT) or "")
        if v:
            return v
        return tree.getpath(p)  # unique XPath fallback

    gps_buckets: Dict[str, Dict] = {}

    for el in tree.iter():
        loc = cgmes_utils.local(el.tag)
        if loc not in cgmes_utils.GPS_X_LOCALS and loc not in cgmes_utils.GPS_Y_LOCALS:
            continue

        parent = el.getparent()
        if parent is None:
            continue

        key = _parent_key(parent)
        bucket = gps_buckets.setdefault(key, {})
        # A valid PositionPoint has exactly one xPosition and one yPosition,
        # so we see each slot exactly once per bucket.
        if loc in cgmes_utils.GPS_X_LOCALS:
            bucket["x_el"] = el
        else:
            bucket["y_el"] = el

    skipped = 0
    for key, bucket in gps_buckets.items():
        x_el = bucket.get("x_el")
        y_el = bucket.get("y_el")
        if x_el is None or y_el is None:
            skipped += 1
            continue
        _apply_gps_pair(
            x_el,
            y_el,
            seed=seed,
            gps_delete=gps_delete,
            gps_transform=gps_transform,
            anonymizer=anonymizer,
            parent_id=key,
        )
    if skipped:
        logger.warning(
            "   %d GPS bucket(s) incomplete (x or y missing) – skipped", skipped
        )


def _anonymize_line_specs(
    tree: etree._ElementTree,
    *,
    anonymizer: utils.SeededNameAnonymizer,
):
    """
    Getting all line information, like length and impedance and alter them.
    """
    mapping: Dict[str, float] = {}
    for el in tree.iter():
        loc = cgmes_utils.local(el.tag)
        if loc not in cgmes_utils.LINE_SPECS_LOCALS:
            continue

        # getting the length of the element
        elem_value = float(el.text)
        cur_rdf_id = cgmes_utils.get_parent_rdfinfo(el, cgmes_utils.RDF_ID)
        alt_factor = utils.get_alteration(
            anonymizer=anonymizer,
            name=loc,
            current_id=cur_rdf_id,
        )
        # length is always set altered by the alteeration
        if loc == "Conductor.length":
            mapping[loc] = elem_value
            new_length = elem_value * alt_factor
            el.text = str(new_length)
            anonymizer.line_mapping.setdefault(
                cur_rdf_id,
                mapping,
            )
            mapping: Dict[str, float] = {}

        # every other spec is an impedance and is therefore slightly altered
        else:

            mapping[loc] = elem_value
            new_impedance = elem_value * alt_factor
            el.text = str(new_impedance)


def _anonymize_text_fields(
    tree: etree._ElementTree,
    *,
    anonymizer: utils.SeededNameAnonymizer,
    desc_delete: bool,
):
    """
    check every element and anonymize it if it falls in the ANON_TEXT_LOCALS
    """
    for el in tree.iter():
        loc = cgmes_utils.local(el.tag)

        if loc not in cgmes_utils.ANON_TEXT_LOCALS:
            continue
        if not el.text or not el.text.strip():
            continue

        raw = el.text.strip()

        # Description / Model.description: optionally delete
        if loc in ("IdentifiedObject.description", "Model.description") and desc_delete:
            el.text = "Deleted"
            continue

        el.text = anonymizer.translate(raw)


def _anonymize_rdf(
    tree: etree._ElementTree,
    *,
    seed: str,
    anonymizer: utils.SeededNameAnonymizer,
    remap_ids: bool,
):
    """
    only applys if remap_ids == True:

    collect all rdf information and anonymize it
    """
    if not remap_ids:
        return

    # Collect all current rdf:IDs in this tree first (for resource fixup)
    all_ids_before: Set[str] = set()
    for el in tree.iter():
        raw = el.get(cgmes_utils.RDF_ID)
        if raw:
            all_ids_before.add(raw)
        raw = el.get(cgmes_utils.RDF_ABOUT)
        if raw:
            all_ids_before.add(cgmes_utils.strip_hash(raw))

    # Build remap table for IDs found in this tree
    for old_id in all_ids_before:
        if old_id not in anonymizer.cim_forward:
            new_id = utils.generate_seeded_uuid(old_id, seed)
            anonymizer.cim_forward[old_id] = new_id

    # Apply remaps
    for el in tree.iter():
        raw_id = el.get(cgmes_utils.RDF_ID)
        if raw_id and raw_id in anonymizer.cim_forward:
            el.set(cgmes_utils.RDF_ID, anonymizer.cim_forward[raw_id])

        raw_about = el.get(cgmes_utils.RDF_ABOUT)
        if raw_about:
            bare = cgmes_utils.strip_hash(raw_about)
            if bare in anonymizer.cim_forward:
                new_bare = anonymizer.cim_forward[bare]
                el.set(
                    cgmes_utils.RDF_ABOUT,
                    "#" + new_bare if raw_about.startswith("#") else new_bare,
                )

        raw_res = el.get(cgmes_utils.RDF_RESOURCE)
        if raw_res:
            bare = cgmes_utils.strip_hash(raw_res)
            if bare in anonymizer.cim_forward:
                new_bare = anonymizer.cim_forward[bare]
                el.set(
                    cgmes_utils.RDF_RESOURCE,
                    "#" + new_bare if raw_res.startswith("#") else new_bare,
                )


def _anonymize_time(
    tree: etree._ElementTree,
    *,
    anonymizer: utils.SeededNameAnonymizer,
):
    """
    Get the TimeStamps and add the random amount of time to it.
    """
    for el in tree.iter():
        loc = cgmes_utils.local(el.tag)
        if loc not in cgmes_utils.TIME_STAMP_LOCALS:
            continue
        cgmes_timestamp = cgmes_utils.cgmes_time_to_epoch(el.text)
        new_epoch_timestamp = anonymizer.add_time(cgmes_timestamp)
        el.text = cgmes_utils.epoch_to_cgmes_time(new_epoch_timestamp)


# ---------------------------------------------------------------------------
# Public entrypoint: anonymize
# ---------------------------------------------------------------------------


def anonymize_cgmes(
    in_path: Path,
    out_path: Path,
    seed: str,
    mapping_out_path: Path,
    anonymizer: utils.SeededNameAnonymizer,
    *,
    desc: bool = False,
    gps: bool = False,
    remap_ids: bool = False,
) -> None:
    """
    Anonymize a CGMES bundle.

    Parameters
    ----------
    in_path          : zip file, folder, or single XML file
    out_path         : output zip / folder / XML
    seed             : deterministic seed string
    mapping_out_path : path for the lookup-table JSON
    desc             : True  -> replace description text with 'Deleted'
                       False -> anonymize via token mapping
    gps              : True  -> set all GPS coords to 0.0
                       False -> apply rotation + per-object jitter
    remap_ids        : False (default) -> rdf:ID / rdf:about / rdf:resource
                                          are left untouched (they are UUIDs)
                       True            -> remap them as well (use when IDs
                                          encode readable names)
    """
    in_path = Path(in_path)
    out_path = Path(out_path)
    mapping_out_path = Path(mapping_out_path)

    logger.info("=== anym_cgmes.py: Start Anonymize ===")

    gps_transform = utils.build_geo_transform(seed)

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
            logger.debug("  Processing %s ...", rel)
            _anonymize_tree(
                tree,
                seed=seed,
                gps_delete=gps,
                gps_transform=gps_transform,
                anonymizer=anonymizer,
                desc_delete=desc,
                remap_ids=remap_ids,
            )
            cgmes_utils.serialise_xml(tree, path)

        out_path.parent.mkdir(parents=True, exist_ok=True)
        cgmes_utils.pack_bundle([(rel, path) for rel, path, _ in trees], out_path)

    utils.save_mapping_json(mapping_out_path, anonymizer)
    logger.info("  Mapping saved   : %s", mapping_out_path)
    logger.info("  Output          : %s", out_path)
    logger.info("  Names anonymized: %d", len(anonymizer.forward))
    logger.info("  rdf:IDs remapped: %d", len(anonymizer.cim_forward))
    logger.info("  GPS entries     : %d", len(anonymizer.gps_mapping))
    logger.info("=== anym_cgmes.py: Done ===")
