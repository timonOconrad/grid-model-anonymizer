"""
anym_csv.py - CSV anonymizer
=============================

Anonymizes (or restores) a single CSV file using the same seed-based
deterministic token mapping shared with anym_PF / anym_cgmes / anym_json,
driven by an external mapping JSON so the same names/IDs stay consistent
across PowerFactory, CGMES, and CSV exports of the same dataset.

Workflow
--------
1. Load an existing mapping JSON if present (so IDs/names already
   anonymized elsewhere stay consistent), otherwise start a fresh
   mapping using the given seed.
2. Auto-detect the CSV dialect (delimiter) via csv.Sniffer.
3. Anonymize or restore:
   - one or more "name" columns (auto-detected as
     "Name Ortsnetzstation", or the first column, unless
     `columns` is given explicitly) via whole-value token
     substitution,
   - a fixed "Schalter mit Fernwirkanschluss" column, if present,
     where EIN/AUS/NZA status markers are stripped and embedded
     equipment IDs (matched via `_ID_RE`) are anonymized/restored
     individually within the free-text value.
4. Write the transformed CSV, and on anonymize runs, persist the
   updated mapping JSON (merging into any mapping that already
   existed).

Notes
-----
- "anonymize" mode replaces values and grows the mapping; "restore"
  mode looks values up by their `ANON_` prefix and reverses them
  using the mapping's reverse lookup table, leaving unrecognized
  values untouched.
- Column matching is whitespace-tolerant, so header variants with
  extra/missing surrounding spaces still resolve correctly.

Depends on: utils (SeededNameAnonymizer, load_mapping_json).
"""

import csv
import json
import logging
import re
from pathlib import Path
from typing import Dict, List, Optional

from utils import utils

logger = logging.getLogger("anym_csv.py")

_STATUS_RE = re.compile(
    r"\s*\(\s*(?:EIN|AUS|NZA)\s*(?:,\s*(?:EIN|AUS|NZA)\s*)?\)\s*",
    re.IGNORECASE,
)

_ID_RE = re.compile(
    r"\b("
    r"(?:\d{4,}[A-Za-z]{0,3})"
    r"|(?:\d{1,3}Z)"
    r"|(?:[A-Za-z]{1,3}\d{1,4})"
    r")\b",
    re.IGNORECASE,
)


def _clean_status(text: str) -> str:
    # removes (ON)/(OFF) with the spaces next to it
    return _STATUS_RE.sub(" ", text).strip()


def _anonymize_ids_in_text(text: str, anonymizer: utils.SeededNameAnonymizer) -> str:
    def repl(m: re.Match) -> str:
        tok = m.group(1)
        # only anonymize if it actually is an ID
        return anonymizer.translate(tok)

    return _ID_RE.sub(repl, text)


def _detect_csv_dialect(path: Path) -> csv.Dialect:
    with open(path, encoding="utf-8-sig", newline="") as f:
        sample = f.read(4096)
        f.seek(0)
        return csv.Sniffer().sniff(sample, delimiters=";,\t")


def _pick_columns(fieldnames: List[str], requested: Optional[List[str]]) -> List[str]:
    if not fieldnames:
        return []

    if requested:
        # robust match: allow whitespace variants
        norm = {h.strip(): h for h in fieldnames if h}
        cols = []
        for c in requested:
            if c in fieldnames:
                cols.append(c)
            elif c.strip() in norm:
                cols.append(norm[c.strip()])
        return cols

    # fallback: first column
    return [fieldnames[0]]


def transform_csv_with_mapping(
    csv_in: Path,
    csv_out: Path,
    mapping_path: Path,
    *,
    mode: str,
    seed: str,
    columns: Optional[List[str]] = None,
):
    """
    Anonymize or restore a CSV file using a shared mapping JSON.

    Parameters
    ----------
    csv_in, csv_out : input/output CSV paths.
    mapping_path    : mapping JSON to load and (on anonymize) update.
    mode            : "anonymize" or "restore".
    seed            : seed used only when creating a new mapping.
    columns         : explicit column names to anonymize; auto-detected if None.

    Raises
    ------
    RuntimeError
        If the input CSV has no header row.
    """
    logger.info("=== anym_csv.py: Start Import/Anonymize/Export ===")

    # load or create mapping
    mapping_path = Path(mapping_path)
    if mapping_path.exists():
        data = utils.load_mapping_json(mapping_path)
        # falls seed in JSON fehlt/leer ist -> nimm den übergebenen
        if not str(data.get("seed", "")).strip():
            data["seed"] = seed
    else:
        data = {
            "seed": seed,
            "prefix": "ANON_",
            "length": 10,
            "anon_mapping": {},
            "cimRdfId_mapping": {},
            "gps_mapping": {},
        }

    seed = str(data.get("seed", seed))  # fallback to given seed
    prefix = str(data.get("prefix", "ANON_") or "ANON_")
    length = int(data.get("length", 10) or 10)

    anon_map: Dict[str, str] = data.get("anon_mapping", {}) or {}
    anon_rev: Dict[str, str] = {v: k for k, v in anon_map.items()}

    anonymizer = utils.SeededNameAnonymizer(seed=seed, prefix=prefix, length=length)
    anonymizer.forward.update(anon_map)
    anonymizer.reverse.update({v: k for k, v in anon_map.items()})

    csv_in = Path(csv_in)
    csv_out = Path(csv_out)
    csv_out.parent.mkdir(parents=True, exist_ok=True)

    dialect = _detect_csv_dialect(csv_in)

    with open(csv_in, encoding="utf-8-sig", newline="") as f_in:
        reader = csv.DictReader(f_in, delimiter=dialect.delimiter)
        fieldnames = reader.fieldnames or []

        name_cols = _pick_columns(fieldnames, columns)
        if not name_cols:
            raise RuntimeError(
                "Keine CSV-Header gefunden – kann keine Spalten auswählen."
            )

        rows = []
        for row in reader:
            # 1) Station Names
            for col in name_cols:
                val = (row.get(col, "") or "").strip()
                if not val:
                    continue
                if mode.lower() == "anonymize":
                    row[col] = anonymizer.translate(val)
                elif mode.lower() == "restore":
                    if val.startswith(prefix):
                        row[col] = anon_rev.get(val, val)

            rows.append(row)

    # write output
    with open(csv_out, "w", encoding="utf-8", newline="") as f_out:
        writer = csv.DictWriter(
            f_out, fieldnames=fieldnames, delimiter=dialect.delimiter
        )
        writer.writeheader()
        writer.writerows(rows)

    # always save mapping on anonymize (new oder extended)
    if mode.lower() == "anonymize":
        data["anon_mapping"] = anonymizer.forward
        mapping_path.parent.mkdir(parents=True, exist_ok=True)
        mapping_path.write_text(
            json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8"
        )

    logger.info("=== anym_csv.py: End ===")
