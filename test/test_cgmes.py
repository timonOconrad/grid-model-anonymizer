"""Round-trip tests for CGMES bundle anonymization and restoration."""

import tempfile
from pathlib import Path
from typing import Dict, List

import pytest

from anym.anym_cgmes import anonymize_cgmes
from restore.restore_cgmes import restore_cgmes
from utils import cgmes_utils, utils


def get_test_examples(path, rdf_id_flag):
    """Extract text, GPS, time, line-spec, and rdf-id values from a CGMES bundle for comparison."""
    with tempfile.TemporaryDirectory() as tmp_str:
        tmp_dir = Path(tmp_str)
    files = cgmes_utils.extract_bundle(path, tmp_dir)
    elems: Dict[str, List] = {
        "Text_fields": [],
        "GPS": [],
        "Times": [],
        "Line_Specs": [],
        "rdf_id": [],
    }
    for _, path in files:
        tree = cgmes_utils.parse_xml(path)

        for el in tree.iter():
            loc = cgmes_utils.local(el.tag)
            if el.text is None:
                continue

            if loc in cgmes_utils.ANON_TEXT_LOCALS:
                elems["Text_fields"].append(el.text)

            elif loc in cgmes_utils.GPS_X_LOCALS or loc in cgmes_utils.GPS_Y_LOCALS:
                elems["GPS"].append(el.text)

            elif loc in cgmes_utils.TIME_STAMP_LOCALS:
                elems["Times"].append(el.text)

            elif loc in cgmes_utils.LINE_SPECS_LOCALS:
                elems["Line_Specs"].append(el.text)

            raw = el.get(cgmes_utils.RDF_ID)
            if raw and rdf_id_flag:
                elems["rdf_id"].append(raw)
                raw = el.get(cgmes_utils.RDF_ABOUT)
            if raw and rdf_id_flag:
                elems["rdf_id"].append(cgmes_utils.strip_hash(raw))
    return elems


flag_list = [(True, True, True), (False, False, False)]
cgmes_3_list = [entry + ("Texas_3",) for entry in flag_list]
cgmes_24_list = [entry + ("Texas_2.4",) for entry in flag_list]
test_list = cgmes_24_list + cgmes_3_list


@pytest.mark.slow
@pytest.mark.parametrize(
    "gps_flag, desc_flag, id_flag, filename",
    test_list,
)
class TestCGMES:
    """Round-trip tests for CGMES anonymization and restoration across
    GPS/desc/id flag combinations."""

    @pytest.mark.dependency(name="test_cgmes_anym")
    def test_cgmes_anym(
        self, gps_flag: bool, desc_flag: bool, id_flag: bool, filename: str
    ):
        """Check that anonymize_cgmes changes all sensitive fields and writes a mapping file."""
        orig_file, anym_file, _, mapping_file = utils.get_test_files(
            filename, ".zip", "cgmes", [gps_flag, desc_flag, id_flag]
        )
        seed = "test_seed"
        anonymizer = utils.SeededNameAnonymizer(seed=seed)
        anonymize_cgmes(
            in_path=orig_file,
            out_path=anym_file,
            seed=seed,
            mapping_out_path=mapping_file,
            anonymizer=anonymizer,
            desc=desc_flag,
            gps=gps_flag,
            remap_ids=id_flag,
        )

        anym_data = get_test_examples(anym_file, id_flag)
        orig_data = get_test_examples(orig_file, id_flag)

        for orig_type, anym_type in zip(orig_data.values(), anym_data.values()):
            for orig_el, anym_el in zip(orig_type, anym_type):
                assert orig_el != anym_el

        for el in anym_data["Text_fields"]:
            assert el.startswith("ANON_") or el == "Deleted"

        assert mapping_file.exists()

    @pytest.mark.dependency(depends=["test_cgmes_anym"])
    def test_cgmes_restore(
        self, gps_flag: bool, desc_flag: bool, id_flag: bool, filename: str
    ):
        """Check that restore_cgmes recovers the original data from the anonymized file."""
        orig_file, anym_file, restore_file, mapping_file = utils.get_test_files(
            filename, ".zip", "cgmes", [gps_flag, desc_flag, id_flag]
        )

        restore_cgmes(
            in_path=anym_file,
            out_path=restore_file,
            mapping_path=mapping_file,
        )

        restore_data = get_test_examples(restore_file, id_flag)
        orig_data = get_test_examples(orig_file, id_flag)

        for (key, orig_type), restore_type in zip(
            orig_data.items(), restore_data.values()
        ):
            for orig_el, restore_el in zip(orig_type, restore_type):

                if restore_el == "Deleted" and desc_flag:
                    continue
                if orig_el.endswith(" "):
                    restore_el += " "
                if key == "Line_Specs":
                    assert orig_el == pytest.approx(utils.format_float(restore_el))
                else:
                    assert orig_el == pytest.approx(restore_el)
        utils.delete_test_data([anym_file, restore_file, mapping_file])
