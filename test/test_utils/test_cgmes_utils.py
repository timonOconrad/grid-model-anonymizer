"""Unit tests for utils.cgems_utils: name anonymization, hashing, UUIDs, and geo helpers."""

from pathlib import Path

import pytest

from utils import cgmes_utils


@pytest.mark.parametrize("suffix", [".zip", ".xml", ""])
def test_extract_bundle(suffix):
    """Check that extract bundle gets xml files without error"""
    test_path = (
        Path(__file__).parent.parent.resolve()
        / "test_data"
        / "general"
        / str("xml_test" + suffix)
    )

    xml_files = cgmes_utils.extract_bundle(test_path, Path("tmp_dir"))
    assert xml_files


def test_extract_empty_bundle():
    """Check that extract bundle returns an empty list when given an empty
    bundle"""
    test_path = (
        Path(__file__).parent.parent.resolve()
        / "test_data"
        / "general"
        / "empty_folder"
    )

    xml_files = cgmes_utils.extract_bundle(test_path, Path("tmp_dir"))
    assert not xml_files


def test_pack_bundle_folder():
    """Check that pack_bundle creates a proper bundle, without error"""
    test_path = (
        Path(__file__).parent.parent.resolve() / "test_data" / "general" / "xml_test"
    )
    output_path = (
        Path(__file__).parent.parent.resolve()
        / "test_data"
        / "general"
        / "empty_folder"
    )
    xml_files = [(file.name, file) for file in test_path.iterdir()]

    cgmes_utils.pack_bundle(xml_files, output_path)
    for el in output_path.iterdir():
        if el.is_file() and el.suffix == ".xml":
            assert (output_path / el.name).exists()
            el.unlink()


def test_closures():
    """Some final closure to delete tmp_files"""
    tmp_path = Path(__file__).parent.parent.parent.resolve() / "tmp_dir"
    for file in tmp_path.iterdir():
        file.unlink()
    tmp_path.rmdir()
