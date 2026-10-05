"""Round-trip tests for JSON anonymization and restoration."""

import json
import sys
from pathlib import Path
from typing import List

import pytest

sys.path.append(".")
from anym import anym_json
from utils import utils

cats = [
    None,
    ["model_name", "event_classification", "station", "feeder"],
    ["event_begin", "event_end", "extract_begin", "extract_end"],
]
files = [
    "json_test_data",
    "json_test_data_nested",
    "json_test_data_nested_list",
]


@pytest.mark.parametrize("file", files)
@pytest.mark.parametrize("categories", cats)
class TestJSON:
    """Round-trip tests for JSON anonymization and restoration across category selections."""

    # def __init__(self):
    #     pass

    @pytest.mark.dependency(name="test_json_anym")
    def test_json_anym(self, categories: List[str] | None, file):
        """Check that anonymization only changes fields in the selected categories."""
        if categories is None:
            orig_file, anym_file, _, mapping_file = utils.get_test_files(
                file,
                ".json",
                "JSON",
                [None],
            )
        else:
            orig_file, anym_file, _, mapping_file = utils.get_test_files(
                file,
                ".json",
                "JSON",
                categories,
            )
        seed = "test_seed"
        anonymizer = utils.SeededNameAnonymizer(seed=seed)
        anym_json.anonymize_json_file(
            input_json=orig_file,
            output_json=anym_file,
            mapping_output=mapping_file,
            anonymizer=anonymizer,
            categories=categories,
        )

        orig_data = anym_json.load_json_file(orig_file)
        anym_data = anym_json.load_json_file(anym_file)

        if isinstance(orig_data, list):
            check_anym_list(orig_data, anym_data, categories)

        elif isinstance(orig_data, dict):
            check_anym_dict(orig_data, anym_data, categories)

        assert mapping_file.exists()

    @pytest.mark.dependency(depends=["test_json_anym"])
    def test_json_restore(self, categories, file):
        """Check that restoring an anonymized JSON file recovers the original values."""
        if categories is None:
            orig_file, anym_file, restore_file, mapping_file = utils.get_test_files(
                file,
                ".json",
                "JSON",
                [None],
            )
        else:
            orig_file, anym_file, restore_file, mapping_file = utils.get_test_files(
                file,
                ".json",
                "JSON",
                categories,
            )

        anym_json.restore_json_anonymization(
            input_json=anym_file,
            output_json=restore_file,
            mapping_input=mapping_file,
        )

        orig_data = anym_json.load_json_file(orig_file)
        restore_data = anym_json.load_json_file(restore_file)
        if isinstance(orig_data, list):
            check_restore_list(orig_data, restore_data)

        elif isinstance(orig_data, dict):
            check_restore_dict(orig_data, restore_data)

        # for orig_data_point, restore_data_point in zip(orig_data, restore_data):
        #     for orig_el, restore_el in zip(
        #         orig_data_point.values(), restore_data_point.values()
        #     ):
        #         assert orig_el == restore_el
        utils.delete_test_data([anym_file, restore_file, mapping_file])


@pytest.mark.parametrize(
    "filename", ["json_test_data", "Faulty_json_test_data", "No_Test_file"]
)
def test_load_json_file(filename):
    file_path = (
        Path(__file__).parent.resolve()
        / "test_data"
        / "JSON"
        / "orig"
        / str(filename + ".json")
    )
    try:
        _ = anym_json.load_json_file(file_path=file_path)
    except FileNotFoundError:
        assert filename == "No_Test_file"
    except json.JSONDecodeError:
        assert filename == "Faulty_json_test_data"


def check_anym_list(orig_data, anym_data, categories):
    for orig_data_point, anym_data_point in zip(orig_data, anym_data):
        if isinstance(orig_data_point, list):
            check_anym_list(orig_data_point, anym_data_point, categories)
        elif isinstance(orig_data_point, dict):
            check_anym_dict(orig_data_point, anym_data_point, categories)
        else:
            raise TypeError("Json is neither a list nor a dict object")


def check_anym_dict(orig_data, anym_data, categories):
    for (key, orig_el), anym_el in zip(orig_data.items(), anym_data.values()):
        if isinstance(orig_el, dict):
            check_anym_dict(orig_el, anym_el, categories)
            continue
        if categories is None:
            assert orig_el != anym_el
            assert anym_el.startswith("ANON_")
        elif key in categories:
            assert orig_el != anym_el
            assert anym_el.startswith("ANON_")
        else:
            assert orig_el == anym_el


def check_restore_list(orig_data, anym_data):
    for orig_data_point, anym_data_point in zip(orig_data, anym_data):
        if isinstance(orig_data_point, list):
            check_restore_list(orig_data_point, anym_data_point)
        elif isinstance(orig_data_point, dict):
            check_restore_dict(orig_data_point, anym_data_point)
        else:
            raise TypeError("Json is neither a list nor a dict object")


def check_restore_dict(orig_data, anym_data):
    for orig_el, anym_el in zip(orig_data.values(), anym_data.values()):
        if isinstance(orig_el, dict):
            check_restore_dict(orig_el, anym_el)
            continue
        if isinstance(orig_el, list):
            check_restore_list(orig_data, anym_data)
            continue
        assert orig_el == anym_el


if __name__ == "__main__":
    # test_obj = TestJSON()
    TestJSON.test_json_restore(
        "x",
        ["model_name", "event_classification", "station", "feeder"],
        "json_test_data_nested_list",
    )
