"""
main.py
=======

Command-line entry point for the anonymizer toolkit.

This script provides a single CLI for anonymizing (and reversing the
anonymization of) PowerFactory, CGMES, and CSV files that describe power
system network data. Depending on the suffix of ``--input_file``, the
script dispatches to the matching backend:

    .pfd         -> anym.anym_PF        (PowerFactory project import/export)
    .zip / .xml  -> anym.anym_cgmes     (CGMES bundle / single CIM/XML file)
    .csv         -> anym.anym_csv       (plain CSV column anonymization)

In "anonymize" mode (default), the script produces:
    - an anonymized output file (network names, descriptions, GPS
      coordinates, and identifiers replaced with deterministic,
      seed-based pseudonyms), and
    - a mapping JSON file recording the original -> anonymized value
      mapping, required to later reverse the process.

In "restore" mode (``--reverse``), the script takes a previously
anonymized file plus its mapping JSON and reconstructs the original
file.

Typical usage:
    python main.py --input_file model.pfd
    python main.py --input_file model.pfd --no-gps --no-desc
    python main.py --input_file model_anonym.pfd --reverse \\
        --mapping_file model_mapping.json
    python main.py --input_file IEEE39.zip --remap-ids
    python main.py --input_file remote.csv --csv-columns "Name,Ort"

Key CLI options:
    --input_file    Required. Path to the .pfd, .zip, .xml, or .csv file
                     to process.
    --output_file   Optional. Destination path; auto-derived from the
                     input filename (e.g. "_anonym" / "_reverse" suffix)
                     if omitted.
    --mapping_file  Optional. Path to the mapping JSON; auto-derived
                     from the input filename if omitted.
    --seed          Deterministic seed used to generate anonymized
                     values (anonymize mode only). Default: "timon123".
    --reverse       Restore the original file from an anonymized file
                     and its mapping JSON, instead of anonymizing.
    --no-gps        Delete GPS coordinates (set to 0,0) instead of
                     applying a coordinate transform + jitter.
    --no-desc       Replace object descriptions with "Deleted" instead
                     of anonymizing their contents.
    --remap-ids     CGMES only. Also remap rdf:ID / rdf:about /
                     rdf:resource values (off by default, since CGMES
                     IDs are typically UUIDs with no readable content).
    --csv-columns   CSV only. Comma-separated list of column names to
                     anonymize (defaults to "Name Ortsnetzstation" or
                     the first column).

Run as a script (``python main.py ...``); prints a summary of the
resolved input/output/mapping paths and the selected mode, then
reports the total runtime on completion."""

import argparse
import logging
import sys
import time
from pathlib import Path

from anym.anym_cgmes import anonymize_cgmes
from anym.anym_csv import transform_csv_with_mapping
from anym.anym_json import anonymize_json_file, restore_json_anonymization
from anym.anym_pf import run_powerfactory_import_export
from restore.restore_cgmes import restore_cgmes
from restore.restore_pf import run_powerfactory_restore
from utils import utils

logger = logging.getLogger("Main.py")


def parse_args():
    """
    Argument Parser to collect paramaters for the main function.
    Args:
        input_file (Path):
            Input file: .pfd, .zip (CGMES bundle), .xml (single CGMES file), or .csv
        seed (str):
            Seed for deterministic anonymization (anonymize mode only)
        output_file (Path):
            Output file (derived automatically if omitted)
        mapping_file (Path):
            Mapping JSON (output when anonymizing, input when restoring).
            Derived automatically if omitted."
        reverse:
            Restore / reverse anonymization instead of anonymizing
        no-gps
            Delete GPS coordinates (set to 0,0) instead of applying transform+jitter
        no-desc
            Replace descriptions with 'Deleted' instead of anonymizing them
        remap-ids
            CGMES only: also remap rdf:ID / rdf:about / rdf:resource values.
            Default: off (CGMES IDs are already UUIDs without readable names).
            Enable if your IDs contain readable substation or asset names.
    """
    parser = argparse.ArgumentParser(
        description="Anonymizer for PowerFactory .pfd, CGMES .zip/.xml, and .csv files"
    )

    parser.add_argument(
        "--input_file",
        type=Path,
        required=True,
        help="Input file: .pfd, .zip (CGMES bundle), .xml (single CGMES file), or .csv",
    )

    parser.add_argument(
        "--seed",
        type=str,
        default="timon123",
        help="Seed for deterministic anonymization (anonymize mode only)",
    )

    parser.add_argument(
        "--verbose",
        dest="verbose",
        action="store_true",
        help="Enable verbose logging (DEBUG level)",
    )

    parser.add_argument(
        "--log_file",
        type=Path,
        action=None,
        help="Output file for the logging output (printed to cmd line if omitted)",
    )

    parser.add_argument(
        "--output_file",
        type=Path,
        default=None,
        help="Output file (derived automatically if omitted)",
    )

    parser.add_argument(
        "--mapping_file",
        type=Path,
        default=None,
        help=(
            "Mapping JSON (output when anonymizing, input when restoring). "
            "Derived automatically if omitted."
        ),
    )

    parser.add_argument(
        "--reverse",
        action="store_true",
        help="Restore / reverse anonymization instead of anonymizing",
    )

    parser.add_argument(
        "--alteration-factor",
        type=float,
        default=5,
        help="How much line impedances should be altered in percent",
    )

    # --- PowerFactory / CGMES flags ---
    parser.add_argument(
        "--no-gps",
        dest="gps",
        action="store_true",
        help="Delete GPS coordinates (set to 0,0) instead of applying transform+jitter",
    )
    parser.set_defaults(gps=False)

    parser.add_argument(
        "--no-desc",
        dest="desc",
        action="store_true",
        help="Replace descriptions with 'Deleted' instead of anonymizing them",
    )
    parser.set_defaults(desc=False)

    parser.add_argument(
        "--remap-ids",
        dest="remap_ids",
        action="store_true",
        help=(
            "CGMES only: also remap rdf:ID / rdf:about / rdf:resource values. "
            "Default: off (CGMES IDs are already UUIDs without readable names). "
            "Enable if your IDs contain readable substation or asset names."
        ),
    )
    parser.set_defaults(remap_ids=False)

    # --- CSV options ---
    parser.add_argument(
        "--csv-columns",
        type=str,
        default=None,
        help=str(
            "Comma-separated CSV column names to anonymize. Default: "
            "'Name Ortsnetzstation' or first column.",
        ),
    )
    # --- JSON Options ---
    parser.add_argument(
        "--json-categories",
        type=str,
        default=None,
        help=str(
            "JSON Category names to anonymize. Default: all categroies.",
        ),
    )
    args = parser.parse_args()

    suf = args.input_file.suffix.lower()
    if suf not in (".pfd", ".csv", ".zip", ".xml", ".json"):
        raise ValueError("Input must be a .pfd, .zip, .xml, or .json file")

    # --- Auto-derive output file ---
    if args.output_file is None:
        stem = args.input_file.stem
        if suf == ".pfd":
            suffix = "_reverse.pfd" if args.reverse else "_anonym.pfd"
        elif suf in (".zip", ".xml"):
            suffix = "_reverse.zip" if args.reverse else "_anonym.zip"
        elif suf in (".csv"):
            suffix = "_reverse.csv" if args.reverse else "_anonym.csv"
        else:
            suffix = "_reverse.json" if args.reverse else "_anonym.json"
        args.output_file = args.input_file.with_name(stem + suffix)

    # --- Auto-derive mapping file ---
    if args.mapping_file is None:
        args.mapping_file = args.input_file.with_name(
            args.input_file.stem + "_mapping.json"
        )

    return args


def _is_cgmes(path: Path) -> bool:
    """True if the file looks like a CGMES bundle (zip or xml)."""
    return path.suffix.lower() in (".zip", ".xml")


def _set_output_verbosity(verbose: bool, log_file: Path):
    if verbose:
        level = logging.DEBUG
    else:
        level = logging.INFO

    if log_file is None:
        logging.basicConfig(level=level, stream=sys.stdout)
    else:
        logging.basicConfig(level=level, filename=log_file, encoding="utf-8")


def _split_columns_and_categories(input_str: str) -> list[str]:
    return [c.strip() for c in input_str.split(",") if c.strip()]


def main():
    """
    CLI entry point.

    Parses arguments, prints a summary of the resolved paths/mode, then
    dispatches to the PowerFactory, CGMES, or CSV anonymize/restore
    backend based on the input file's suffix.
    """
    args = parse_args()

    try:
        _set_output_verbosity(args.verbose, args.log_file)
    except AttributeError:
        # argparse doesn't set this attribute if the flag is omitted
        _set_output_verbosity(False, args.log_file)

    logger.info("Starting anonymizer toolkit...")

    logger.debug("input_file  :%s", args.input_file)
    logger.debug("output_file :%s", args.output_file)
    logger.debug("mapping_file:%s", args.mapping_file)
    logger.debug("mode        :%s", "restore" if args.reverse else "anonymize")

    suf = args.input_file.suffix.lower()

    anonymizer = utils.SeededNameAnonymizer(
        seed=args.seed, alteration_factor=args.alteration_factor
    )

    # ------------------------------------------------------------------ PFD
    if suf == ".pfd":
        logger.debug("seed        :%s", args.seed)
        logger.debug("desc (True=delete):%s", args.desc)
        logger.debug("gps  (True=delete):%s", args.gps)

        if args.reverse:
            run_powerfactory_restore(
                in_path=args.input_file,
                out_path=args.output_file,
                mapping_path=args.mapping_file,
            )
        else:
            run_powerfactory_import_export(
                in_path=args.input_file,
                out_path=args.output_file,
                random_seed=args.seed,
                mapping_out_path=args.mapping_file,
                anonymizer=anonymizer,
                desc=args.desc,
                gps=args.gps,
                remap_ids=args.remap_ids,
            )

    # ----------------------------------------------------------------- CGMES
    elif _is_cgmes(args.input_file):
        logger.debug("seed        :%s", args.seed)
        logger.debug("desc (True=delete):%s", args.desc)
        logger.debug("gps  (True=delete):%s", args.gps)

        if args.reverse:
            restore_cgmes(
                in_path=args.input_file,
                out_path=args.output_file,
                mapping_path=args.mapping_file,
            )
        else:
            anonymize_cgmes(
                in_path=args.input_file,
                out_path=args.output_file,
                seed=args.seed,
                mapping_out_path=args.mapping_file,
                anonymizer=anonymizer,
                desc=args.desc,
                gps=args.gps,
                remap_ids=args.remap_ids,
            )

    # ------------------------------------------------------------------ CSV
    elif suf == ".csv":
        cols = None
        if args.csv_columns:
            cols = _split_columns_and_categories(args.csv_columns)

        transform_csv_with_mapping(
            csv_in=args.input_file,
            csv_out=args.output_file,
            mapping_path=args.mapping_file,
            mode="restore" if args.reverse else "anonymize",
            seed=args.seed,
            columns=cols,
        )

    elif suf == ".json":
        if args.json_categories:
            categories = _split_columns_and_categories(args.json_categories)
        else:
            categories = None

        if args.reverse:
            restore_json_anonymization(
                input_json=args.input_file,
                output_json=args.output_file,
                mapping_input=args.mapping_file,
            )
        else:
            anonymize_json_file(
                input_json=args.input_file,
                output_json=args.output_file,
                mapping_output=args.mapping_file,
                anonymizer=anonymizer,
                categories=categories,
            )
    else:
        logger.error("File type not supported yet.")


if __name__ == "__main__":
    start = time.time()
    main()
    logger.info(str(f"Duration: {time.time() - start:.2f} s"))


# ---------------------------------------------------------------------------
# Usage examples
# ---------------------------------------------------------------------------
# Anonymize CGMES bundle (GPS transformed, descriptions anonymized):
#   python main.py --input_file IEEE39.zip
#
# Anonymize + delete GPS + delete descriptions:
#   python main.py --input_file IEEE39.zip --no-gps --no-desc
#
# Restore CGMES:
#   python main.py --input_file IEEE39_anonym.zip --reverse --mapping_file IEEE39_mapping.json
#
# Anonymize PFD:
#   python main.py --input_file model.pfd
#
# Anonymize PFD (delete GPS, delete desc):
#   python main.py --input_file model.pfd --no-gps --no-desc
#
# Restore PFD:
#   python main.py --input_file model_anonym.pfd --reverse --mapping_file model_mapping.json
#
# CSV:
#   python main.py --input_file remote_anonym.csv --reverse --mapping_file model_mapping.json
