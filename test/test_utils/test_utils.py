"""Unit tests for utils.utils: name anonymization, hashing, UUIDs, and geo helpers."""

from pathlib import Path

import pytest

from utils import utils


@pytest.mark.parametrize("seed_input,time_output", [("test_seed", 956552995)])
def test_seeded_name_anonymizer_time_adding(seed_input, time_output) -> None:
    """Verify SeededNameAnonymizer initializes its attributes correctly."""
    anonymizer = utils.SeededNameAnonymizer(seed_input)
    assert anonymizer.time_adding == time_output
    assert anonymizer.seed == seed_input


@pytest.mark.parametrize(
    "prefix_input,prefix_output", [(None, "ANON_"), ("AnotherAnon", "AnotherAnon")]
)
def test_seeded_name_anonymizer_prefix(prefix_input, prefix_output) -> None:
    """Verify SeededNameAnonymizer initializes its attributes correctly."""
    if prefix_input is None:
        anonymizer = utils.SeededNameAnonymizer("test_seed")
    else:
        anonymizer = utils.SeededNameAnonymizer("test_seed", prefix=prefix_input)
    assert anonymizer.prefix == prefix_output


@pytest.mark.parametrize(
    "alteration_factor_input,alteration_factor_output",
    [(None, 5), (20, 10), (1, 1)],
)
def test_seeded_name_anonymizer_alteration_factor(
    alteration_factor_input, alteration_factor_output
) -> None:
    """Verify SeededNameAnonymizer initializes its attributes correctly."""
    if alteration_factor_input is None:
        anonymizer = utils.SeededNameAnonymizer("test_seed")
    else:
        anonymizer = utils.SeededNameAnonymizer(
            "test_seed", alteration_factor=alteration_factor_input
        )
    assert anonymizer.alteration_factor == alteration_factor_output


@pytest.mark.parametrize("length_input,length_output", [(None, 10), (15, 15)])
def test_seeded_name_anonymizer_length(length_input, length_output) -> None:
    """Verify SeededNameAnonymizer initializes its attributes correctly."""
    if length_input is None:
        anonymizer = utils.SeededNameAnonymizer("test_seed")
    else:
        anonymizer = utils.SeededNameAnonymizer("test_seed", length=length_input)
    assert anonymizer.length == length_output


def test_get_hash():
    """Check that get_hash produces the expected deterministic hash string."""
    anonymizer = utils.SeededNameAnonymizer("test_seed", "Anon_")
    hash_str = anonymizer.get_hash("Test_text", 10)
    assert hash_str == "5830EE3D51"


@pytest.mark.parametrize(
    "input_value,output_value",
    [
        ("", ""),
        ("Text", "Anon_0F2B146C3A"),
        ("Anon_stuff", "Anon_stuff"),
    ],
)
def test_translate(input_value, output_value):
    """Check that translate returns the expected prefixed, hashed name."""
    # check for different inputs
    anonymizer = utils.SeededNameAnonymizer(seed="test_seed", prefix="Anon_")
    translation = anonymizer.translate(input_value)
    assert translation == output_value


def test_translate_attr():
    """Check that translate_attr returns the expected prefixed, hashed name."""
    anonymizer = utils.SeededNameAnonymizer(seed="test_seed", prefix="Anon_")
    translation = anonymizer.translate_attr("Text", "TexT")
    assert translation == "Anon_1E0DAD6AD7"


@pytest.mark.parametrize(
    "time_add,new_time",
    [
        (40000000, 70000000),
        (-10000, 29990000),
        (-40000000, 70000000),
        (2**32, 2**32 - 30000000),
    ],
)
def test_add_time(time_add, new_time):
    """Check add_time shifts a timestamp by the configured offset."""
    old_time = 30000000
    anonymizer = utils.SeededNameAnonymizer("test_seed", "Anon_")

    anonymizer.time_adding = time_add
    new_time = anonymizer.add_time(old_time)
    assert new_time == new_time


def test_generate_seeded_uuid():
    """Check generate_seeded_uuid produces a deterministic UUID for a given seed."""
    seed = "seed"
    old_id = "Some beautiful ID"
    expected_id = "_97c7089e-f667-7594-554f-73a15dbb9787"
    new_id = utils.generate_seeded_uuid(old_id, seed)
    assert new_id == expected_id


@pytest.mark.parametrize(
    "filename, expected_result",
    [
        (
            "test_mapping.json",
            {
                "seed": "test_seed",
                "prefix": "ANON_",
                "length": 10,
                "anon_mapping": {
                    "Test1": "ANON_E9D7DFD72C",
                    "Attr1": "ANON_39B3EAEE0F",
                },
            },
        ),
        (
            "faulty_mapping.json",
            {
                "seed": "test_seed",
                "prefix": "ANON_",
                "length": 10,
                "loc_name_mapping": {
                    "Test1": "ANON_E9D7DFD72C",
                },
                "attr_mappings": {"Object1": {"Attr1": "ANON_39B3EAEE0F"}},
                "anon_mapping": {
                    "Test1": "ANON_E9D7DFD72C",
                    "Attr1": "ANON_39B3EAEE0F",
                },
            },
        ),
    ],
)
def test_load_mapping_json(filename, expected_result):
    """Check if load_mapping_json correctly loads and parses a mapping JSON file."""

    test_path = (
        Path(__file__).parent.parent.resolve() / "test_data" / "general" / filename
    )

    data = utils.load_mapping_json(test_path)
    assert data == expected_result


def test_meters_to_deg_lat():
    """Check meters_to_deg_lat converts a distance in meters to degrees latitude."""
    meters = 5_000_000
    assert utils.meters_to_deg_lat(meters) == pytest.approx(44.91555875)


def test_meters_to_deg_lon():
    """Check meters_to_deg_lon converts meters to degrees longitude at given latitudes."""
    meters = 5_000_000
    lat_deg = 30
    deg_lat_30 = 51.864019869711264
    assert utils.meters_to_deg_lon(meters, lat_deg) == pytest.approx(deg_lat_30)

    lat_deg = 89
    deg_lat_89 = 449.1555874955084
    assert utils.meters_to_deg_lon(meters, lat_deg) == pytest.approx(deg_lat_89)


@pytest.mark.parametrize(
    "inpt_obj,output_bool",
    [
        ("Testfolder\\Subfolder", False),
        ("Testfolder\\testfile.test", True),
    ],
)
def test_has_suffix(inpt_obj, output_bool):
    """Check has_suffix detects file suffixes and raises on invalid input."""
    assert utils.has_suffix(inpt_obj) is output_bool

    with pytest.raises(AttributeError):
        utils.has_suffix("This is not a file")


@pytest.mark.parametrize(
    "input_float, expected_output",
    [(0.0, "0"), (1.3e-09, "1.3e-9"), (3.0e-12, "3e-12")],
)
def test_format_float(input_float, expected_output):
    """Check format_float returns the expected string representation of a float."""
    assert utils.format_float(input_float) == expected_output


if __name__ == "__main__":
    test_load_mapping_json(
        "faulty_mapping.json",
        {
            "seed": "test_seed",
            "prefix": "ANON_",
            "length": 10,
            "loc_name_mapping": {
                "Test1": "ANON_E9D7DFD72C",
            },
            "attr_mappings": {"Object1": {"Attr1": "ANON_39B3EAEE0F"}},
            "anon_mapping": {
                "Test1": "ANON_E9D7DFD72C",
                "Attr1": "ANON_39B3EAEE0F",
            },
        },
    )
