"""Round-trip tests for CSV anonymization and restoration."""

import csv
from typing import Dict, List, Optional

import pytest

from anym import anym_csv
from utils import utils

# pylint: disable=protected-access


def get_example_data(csv_in, columns: Optional[List[str]] = None):
    """Read the given CSV and collect values from the selected name columns for comparison."""
    dialect = anym_csv._detect_csv_dialect(csv_in)

    with open(csv_in, encoding="utf-8-sig", newline="") as f_in:
        reader = csv.DictReader(f_in, delimiter=dialect.delimiter)

        fieldnames = reader.fieldnames or []

        name_cols = anym_csv._pick_columns(fieldnames, columns)
        elems: Dict[str, List] = {key: [] for key in name_cols}
        for row in reader:
            for col in name_cols:
                val = (row.get(col, "") or "").strip()
                elems[col].append(val)
        return elems


@pytest.mark.parametrize(
    "columns", [["Name Ortsnetzstation"], ["Schalter mit Fernwirkanschluss"], None]
)
class TestCSV:
    """Round-trip tests for CSV anonymization and restoration with different column selections."""

    @pytest.mark.dependency(name="test_csv_anym")
    def test_csv_anym(self, columns):
        """Check that CSV anonymization changes selected columns and writes a mapping file."""
        orig_file, anym_file, _, mapping_file = utils.get_test_files(
            "csv_test", ".csv", "CSV", [columns]
        )
        seed = "test_seed"
        anym_csv.transform_csv_with_mapping(
            csv_in=orig_file,
            csv_out=anym_file,
            mapping_path=mapping_file,
            mode="anonymize",
            seed=seed,
            columns=columns,
        )

        orig_data = get_example_data(orig_file, columns)
        anym_data = get_example_data(anym_file, columns)

        for orig_type, anym_type in zip(orig_data.values(), anym_data.values()):
            for orig_el, anym_el in zip(orig_type, anym_type):
                # if columns is None or orig_key in columns:
                assert (orig_el != anym_el) or (orig_el == "")
                assert anym_el.startswith("ANON_") or anym_el == ""
        assert mapping_file.exists()

    @pytest.mark.dependency(depends=["test_csv_anym"])
    def test_csv_restore(self, columns):
        """Check that restoring an anonymized CSV recovers the original column values."""
        orig_file, anym_file, restore_file, mapping_file = utils.get_test_files(
            "csv_test", ".csv", "CSV", [columns]
        )
        seed = "test_seed"

        anym_csv.transform_csv_with_mapping(
            csv_in=anym_file,
            csv_out=restore_file,
            mapping_path=mapping_file,
            mode="restore",
            seed=seed,
            columns=columns,
        )
        orig_data = get_example_data(orig_file, columns)
        restore_data = get_example_data(restore_file, columns)

        for orig_type, restore_type in zip(orig_data.values(), restore_data.values()):
            for orig_el, restore_el in zip(orig_type, restore_type):
                assert orig_el == restore_el
        assert mapping_file.exists()
        utils.delete_test_data([anym_file, restore_file, mapping_file])
