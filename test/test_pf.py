"""Round-trip tests for PowerFactory project anonymization and restoration."""

import csv
import itertools
import json
import logging
import math
import statistics
import sys
import time
from pathlib import Path
from typing import Dict

import matplotlib.pyplot as plt
import pytest
from matplotlib.patches import Patch

sys.path.append(str(Path(__file__).parent.parent.resolve()))
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


TEST_FILE = "LV Distribution Network"


@pytest.mark.slow
@pytest.mark.parametrize(
    "gps_flag, desc_flag, id_flag, test_file",
    [(True, True, True, TEST_FILE), (False, False, False, TEST_FILE)],
)
class TestPowerFactory:
    """Round-trip tests for PowerFactory project anonymization and restoration,
    requiring PowerFactory."""

    @pytest.mark.dependency(name="test_powerfactory_anym")
    def test_powerfactory_anym(
        self, gps_flag: bool, desc_flag: bool, id_flag: bool, test_file: str
    ):
        """Check that anonymizing a PowerFactory project writes a mapping
        file (skipped if PF is absent)."""
        if pf_utils.get_pf_version() is False:
            pytest.skip("No PowerFactory installed")

        orig_file, anym_file, _, mapping_file = utils.get_test_files(
            test_file,
            ".pfd",
            "PowerFactory",
            [gps_flag, desc_flag, id_flag],
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
    def test_powerfactory_restore(
        self, gps_flag: bool, desc_flag: bool, id_flag: bool, test_file: str
    ):
        """Check that restoring an anonymized PowerFactory project recovers
        original attribute values."""
        if pf_utils.get_pf_version() is False:
            pytest.skip("No PowerFactory installed")

        orig_file, anym_file, restore_file, mapping_file = utils.get_test_files(
            test_file,
            ".pfd",
            "PowerFactory",
            [gps_flag, desc_flag, id_flag],
        )

        restore_pf.run_powerfactory_restore(
            in_path=anym_file,
            out_path=restore_file,
            mapping_path=mapping_file,
            project_name=test_file,
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
                "14 Bus System(1)",
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
    _, anon_rev, _, _, _, _, prefix, _, _ = utils.get_mappings(mapping_path)

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
    assert square_error != 0
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


def get_load_flow_diff_plots():

    if pf_utils.get_pf_version() is False:
        pytest.skip("No PowerFactory installed")

    seed = "test_seed"
    paths = [
        "Nine-bus System",
        "14 Bus System(1)",
        "39 Bus New England System",
    ]
    alt_factors = [0, 1, 3, 5, 10]

    load_flow_results = {}
    calc_times = {}
    for path in paths:
        load_flow_results[path] = {}
        times = [0] * len(alt_factors)
        orig_path, anym_path, _, mapping_path = utils.get_test_files(
            path, ".pfd", "PowerFactory", []
        )
        app = pf.GetApplication()
        orig_ldf_results = pf_utils.get_load_flow_results(
            app, orig_path, anon_rev=None, prefix="Anon_"
        )
        load_flow_results[path]["orig"] = orig_ldf_results
        for idx, alt_factor in enumerate(alt_factors):
            start = time.time()
            anonymizer = utils.SeededNameAnonymizer(
                seed=seed, alteration_factor=alt_factor
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
            times[idx] = time.time() - start
            _, anon_rev, _, _, _, _, prefix, _, _ = utils.get_mappings(mapping_path)

            anym_ldf_results = pf_utils.get_load_flow_results(
                app, anym_path, anon_rev, prefix
            )

            load_flow_results[path][f"Factor: {alt_factor}"] = anym_ldf_results

            utils.delete_test_data([anym_path, mapping_path])

        calc_times[path] = statistics.mean(times)

    # ------------------------------ Courtesy of Claude -----------------------
    # data type -> (title, y-label, category in JSON, value key)
    types = {
        "generator_loading": (
            "Generator Loading",
            "Δ Loading [%]",
            "generators",
            "loading",
        ),
        "line_loading": ("Line Loading", "Δ Loading [%]", "lines", "loading"),
        "bus_voltage": ("Bus Voltage", "Δ Voltage [p.u.]", "busses", "u"),
        "bus_angle": ("Bus Angle", "Δ Angle [°]", "busses", "deg"),
    }

    # Scale factors to per unit (used only for the "all data" plot):
    # loading [%] -> p.u. (/100), voltage is already p.u., angle [°] -> rad
    pu_scale = {
        "generators": {"loading": 1 / 100},
        "lines": {"loading": 1 / 100},
        "busses": {"u": 1.0, "deg": math.pi / 180},
    }

    colors = plt.rcParams["axes.prop_cycle"].by_key()["color"]

    def deviations(project, factor, category, key):
        """Deviation (factor - orig) per element of a project."""
        orig = load_flow_results[project]["orig"][category]
        fac = load_flow_results[project][factor][category]
        return [fac[name][key] - orig[name][key] for name in orig]

    def value_range(category, key):
        """Min/max of all deviations for one data type (with a small margin)."""
        vals = [
            v
            for project, runs in load_flow_results.items()
            for factor in runs
            if factor != "orig"
            for v in deviations(project, factor, category, key)
        ]
        margin = 0.05 * (max(vals) - min(vals))
        return min(vals) - margin, max(vals) + margin

    def plot(types, title, ylabel, filename, to_pu=False, ylim=None):
        fig, ax = plt.subplots(figsize=(12, 5))
        pos = 0
        group_centers, group_names = [], []
        factor_names = []
        for project, runs in load_flow_results.items():
            factors = [k for k in runs if k != "orig"]
            start = pos
            for i, factor in enumerate(factors):
                vals = []
                for category, key in types:
                    scale = pu_scale[category][key] if to_pu else 1.0
                    vals += [
                        v * scale for v in deviations(project, factor, category, key)
                    ]
                bp = ax.boxplot(vals, positions=[pos], widths=0.8, patch_artist=True)
                bp["boxes"][0].set_facecolor(colors[i % len(colors)])
                for m in bp["medians"]:
                    m.set_color("black")
                if factor not in factor_names:
                    factor_names.append(factor)
                pos += 1
            group_centers.append((start + pos - 1) / 2)
            group_names.append(project)
            pos += 1  # gap between project groups

        ax.set_xticks(group_centers)
        ax.set_xticklabels(group_names)
        ax.set_xlabel("Original project")
        ax.set_ylabel(ylabel)
        ax.set_title(title)
        if ylim is not None:
            ax.set_ylim(ylim)
        ax.axhline(0, color="gray", linewidth=0.8, linestyle="--")
        ax.grid(axis="y", alpha=0.3)

        # light legend: outside the plot, no frame, small font
        ax.legend(
            handles=[
                Patch(
                    facecolor=colors[i % len(colors)],
                    edgecolor="black",
                    linewidth=0.8,
                    label=f.replace("Factor: ", ""),
                )
                for i, f in enumerate(factor_names)
            ],
            title="Factor",
            loc="upper left",
            bbox_to_anchor=(1.01, 1),
            frameon=False,
            fontsize=9,
            title_fontsize=9,
            handlelength=1.0,
            handleheight=1.0,
        )
        fig.tight_layout()
        fig.savefig(filename, dpi=150)
        plt.close(fig)

    # Shared y-axis for generator and line loading (same unit: % loading)
    loading_lo, loading_hi = zip(
        value_range("generators", "loading"), value_range("lines", "loading")
    )
    LOADING_YLIM = (min(loading_lo), max(loading_hi))
    SHARED_YLIM = {"generator_loading": LOADING_YLIM, "line_loading": LOADING_YLIM}

    # One image per data type
    for name, (title, ylabel, category, key) in types.items():
        plot(
            [(category, key)],
            f"Deviation from original: {title}",
            ylabel,
            f"{name}.png",
            ylim=SHARED_YLIM.get(name),
        )

    # One image over all data
    plot(
        [(c, k) for _, _, c, k in types.values()],
        "Deviation from original: all data types",
        "Δ [p.u.]",
        "all_data.png",
        to_pu=True,
    )

    # Statistics -> CSV
    # mean_original: mean of the original values (original units)
    # mean_value: mean of the values remaining at this factor (original units)
    # std_deviation: std of the deviations (factor - orig) over all elements of the project
    rows = []
    for project, runs in load_flow_results.items():
        factors = [k for k in runs if k != "orig"]
        for factor in factors:
            factor_label = factor.replace("Factor: ", "")

            # one row per data type (original units)
            for name, (title, ylabel, category, key) in types.items():
                dev = deviations(project, factor, category, key)
                rows.append(
                    {
                        "project": project,
                        "data_type": title,
                        "factor": factor_label,
                        "unit": ylabel.split("[")[1].rstrip("]"),
                        "mean_deviation": statistics.mean(dev),
                        "std_deviation": statistics.stdev(dev),
                    }
                )

            # all data types together (p.u.)
            dev_all = []
            for category, key in [(c, k) for _, _, c, k in types.values()]:
                scale = pu_scale[category][key]
                dev_all += [
                    v * scale for v in deviations(project, factor, category, key)
                ]
            rows.append(
                {
                    "project": project,
                    "data_type": "All data types",
                    "factor": factor_label,
                    "unit": "p.u.",
                    "mean_deviation": statistics.mean(dev_all),
                    "std_deviation": statistics.stdev(dev_all),
                }
            )

    with open("statistics.csv", "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
    with open("calc_times.json", "w", encoding="utf-8") as f:
        json.dump(calc_times, f)


# ------------------------------ Courtesy of Claude -----------------------

if __name__ == "__main__":
    project_dir = Path(__file__).parent.parent.resolve()
    test_dir = Path(project_dir, "test")
    data_dir = Path(test_dir, "test_data", "PowerFactory")
    the_file = Path(data_dir, "orig", "39 Bus New England System.pfd")
    get_load_flow_diff_plots()
