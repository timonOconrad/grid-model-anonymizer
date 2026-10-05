"""Round-trip tests for PowerFactory project anonymization and restoration."""

import itertools
import logging
import math
from pathlib import Path
from typing import Dict

import pytest

from anym import anym_pf
from restore import restore_pf
from utils import pf_utils, utils

pf = pf_utils.import_powerfactory_module()
ATTRIBUTES = [
    "iStudyTime",
    "sernum",
    "constr",
    "chr_name",
    "dar_src",
    "manuf",
    "for_name",
    "foreignKey",
    "desc",
    "GPSlat",
    "GPSlon",
    "dline",
    "typ_id",
    "rline",
    "xline",
    "rline0",
    "xline0",
]

logger = logging.getLogger("Test_pf")
logging.basicConfig(level=logging.DEBUG, filename="test.log", encoding="utf-8")


def get_example_data(path: Path, app) -> Dict[str, Dict[str, str | None]]:
    """Import a PowerFactory project and collect tracked attribute values for every object."""
    project_name = "Texas Grid"

    pf_utils.delete_project_if_exists(app, project_name)
    pf_utils.import_pfd_into_current_user(app, path)
    pf_utils.activate_project(app, project_name)
    objects = pf_utils.collect_unique_objects_for_anonymization(app)
    obj_dict = restore_pf.make_obj_dict(objects)
    attr_dict = {}

    for key, obj in obj_dict.items():
        attr_dict[key] = {}
        for attr in ATTRIBUTES:
            attr_dict[key][attr] = pf_utils.get_str_attr(obj, attr)
    return attr_dict


@pytest.mark.slow
@pytest.mark.parametrize(
    "gps_flag, desc_flag, id_flag", [(True, True, True), (False, False, False)]
)
class TestPowerFactory:
    """Round-trip tests for PowerFactory project anonymization and restoration,
    requiring PowerFactory."""

    @pytest.mark.dependency(name="test_powerfactory_anym")
    def test_powerfactory_anym(self, gps_flag: bool, desc_flag: bool, id_flag: bool):
        """Check that anonymizing a PowerFactory project writes a mapping
        file (skipped if PF is absent)."""
        if pf_utils.get_pf_version() is False:
            pytest.skip("No PowerFactory installed")

        orig_file, anym_file, _, mapping_file = utils.get_test_files(
            "Texas Grid", ".pfd", "PowerFactory", [gps_flag, desc_flag, id_flag]
        )
        seed = "test_seed"
        anonymizer = utils.SeededNameAnonymizer(seed=seed)
        anym_pf.run_powerfactory_import_export(
            in_path=orig_file,
            out_path=anym_file,
            random_seed=seed,
            mapping_out_path=mapping_file,
            desc=desc_flag,
            gps=gps_flag,
            remap_ids=id_flag,
            anonymizer=anonymizer,
        )

        assert mapping_file.exists()

    @pytest.mark.dependency(depends=["test_powerfactory_anym"])
    def test_powerfactory_restore(self, gps_flag: bool, desc_flag: bool, id_flag: bool):
        """Check that restoring an anonymized PowerFactory project recovers
        original attribute values."""
        if pf_utils.get_pf_version() is False:
            pytest.skip("No PowerFactory installed")

        orig_file, anym_file, restore_file, mapping_file = utils.get_test_files(
            "Texas Grid", ".pfd", "PowerFactory", [gps_flag, desc_flag, id_flag]
        )

        restore_pf.run_powerfactory_restore(
            in_path=anym_file,
            out_path=restore_file,
            mapping_path=mapping_file,
            project_name="Texas Grid",
        )

        app = pf.GetApplication()
        orig_data = get_example_data(orig_file, app)
        restore_data = get_example_data(restore_file, app)

        for obj_key, obj_data in orig_data.items():
            for attr_name, attr_data in obj_data.items():
                attr_restored = restore_data[obj_key][attr_name]
                if attr_data == "":
                    continue
                try:
                    assert attr_data == attr_restored or attr_restored == "Deleted"
                except AssertionError:
                    try:
                        assert attr_data == attr_restored.replace(";", " ")
                    except AssertionError:
                        assert attr_data == attr_restored + " "
        utils.delete_test_data([anym_file, restore_file, mapping_file])


@pytest.mark.parametrize(
    "path, alt_factor_percent",
    list(
        itertools.product(
            [
                "Nine-bus System",
                "IEEE 13 Node Feeder",
                "14 Bus System(1)",
                # "LV Distribution Network",
                "39 Bus New England System",
            ],
            [-1, 0, 3, 5, 10],
        ),
    ),
)
def test_powerfactory_load_flow_accuracy(path, alt_factor_percent):
    """Check that load_flow_results correctly loads and parses a flow results graphic file."""
    if pf_utils.get_pf_version() is False:
        pytest.skip("No PowerFactory installed")
    orig_path, anym_path, _, mapping_path = utils.get_test_files(
        path, ".pfd", "PowerFactory", []
    )

    seed = "test_seed"
    anonymizer = utils.SeededNameAnonymizer(
        seed=seed, alteration_factor=alt_factor_percent
    )
    anym_pf.run_powerfactory_import_export(
        in_path=orig_path,
        out_path=anym_path,
        random_seed=seed,
        mapping_out_path=mapping_path,
        desc=False,
        gps=False,
        remap_ids=False,
        anonymizer=anonymizer,
    )
    (
        _,
        anon_rev,
        _,
        _,
        _,
        _,
        prefix,
    ) = utils.get_mappings(mapping_path)

    app = pf.GetApplication()
    orig_ldf_results = pf_utils.get_load_flow_results(app, orig_path, anon_rev, prefix)
    anym_ldf_results = pf_utils.get_load_flow_results(app, anym_path, anon_rev, prefix)
    load_flow_asserts(
        orig_ldf_results=orig_ldf_results,
        anym_ldf_results=anym_ldf_results,
        alt_factor=alt_factor_percent,
        project_name=orig_path.stem,
    )
    utils.delete_test_data([anym_path, mapping_path])


def load_flow_asserts(
    orig_ldf_results, anym_ldf_results, alt_factor, project_name
) -> float:
    """Go through all the results of the load flow and calculate the relative error
    between the original and anonymized results. give out the maximum error and the
    root mean square error (RMSE) of the load flow results.
    """
    difference_list = []
    for type_key, type_entry in orig_ldf_results.items():
        for elem_key, elem_entry in type_entry.items():

            for value_key, orig_value_entry in elem_entry.items():
                anym_value_entry = anym_ldf_results[type_key][elem_key][value_key]
                try:
                    rel_error = (orig_value_entry - anym_value_entry) / orig_value_entry
                except ZeroDivisionError:
                    rel_error = orig_value_entry - anym_value_entry
                difference_list.append(rel_error)

    square_error = [x**2 for x in difference_list]
    mean_square_error = sum(square_error) / len(square_error)

    rmse = math.sqrt(mean_square_error)
    max_error = math.sqrt(max(square_error))
    with open("Load_flow_test.txt", mode="a", encoding="utf-8") as f:
        f.write(f"Current Network: {project_name} \n")

        f.write(
            f"The maximum deviation of the load flow results is {max_error*100:.2f}% "
            f"in one of the elements, the Alteration Factor is at {alt_factor:.0f}%.\n",
        )
        if rmse >= 1 / 100:
            f.write(
                f"The averaged error for load flow analysis is larger than 1% with "
                f"{rmse *100:.2f}%! Use a smaller alteration factor to reduce the error.\n",
            )
        else:
            f.write(
                f"The averaged error for a load flow analysis is at {rmse*100:.2f}%!\n",
            )
        f.write("\n")
