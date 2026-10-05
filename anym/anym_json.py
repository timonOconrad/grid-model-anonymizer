"""
anym_json.py - JSON anonymizer
=============================

Anonymizes (or restores) a JSON file (a list of dict-like entries) using
the same seed-based deterministic token mapping shared with anym_PF /
anym_cgmes / anym_csv, driven by an external mapping JSON so the same
names/IDs stay consistent across exports of the same dataset.

Workflow
--------
1. Load the input JSON file (expected: a list of dict entries).
2. Determine which keys ("categories") to anonymize:
   - use the explicitly given `categories` list, or
   - if none is given, auto-detect all keys occurring anywhere across
     the entries (via `_get_json_keys`).
3. Anonymize: for every entry, replace the value of each present
   category key with a deterministic, seed-based token via
   SeededNameAnonymizer.
4. Write the transformed JSON to the output path, and persist the
   generated mapping JSON.
5. Restore: reverse the process using a previously saved mapping -
   any string value starting with the mapping's prefix (`ANON_` by
   default) is looked up and replaced with its original value,
   regardless of which key it appears under.

Notes
-----
- "anonymize" mode replaces values and grows the mapping; "restore"
  mode looks values up by their `ANON_` prefix and reverses them
  using the mapping's reverse lookup table, leaving unrecognized
  values untouched.
- Only string values are touched; other types (numbers, booleans,
  nested objects/lists) are left as-is.
- Auto-detecting categories (step 2, no `categories` given) is
  convenient for unknown JSON structures but anonymizes every key
  found in the data - pass an explicit `categories` list to limit
  anonymization to specific fields.

Depends on: utils (SeededNameAnonymizer, load_mapping_json, save_mapping_json).
"""

import json
import logging
from pathlib import Path
from typing import List, Optional

from utils import utils

logger = logging.getLogger("anym_json.py")


def load_json_file(file_path: str):
    """
    Load a JSON file and return its content as a Python object.

    Parameters
    ----------
    file_path : str
        The path to the JSON file to be loaded.

    Returns
    -------
    dict or list
        The content of the JSON file as a Python dictionary or list.

    Raises
    ------
    FileNotFoundError
        If the specified file does not exist.
    json.JSONDecodeError
        If the file is not a valid JSON.
    """
    logger.debug("Loading Json file: %s", file_path)
    try:
        with open(file_path, "r", encoding="utf-8") as file:
            data = json.load(file)
            return data
    except FileNotFoundError:
        logger.error("File not found: '%s'", file_path)
        raise
    except json.JSONDecodeError:
        logger.error("Invalid JSON format in file: '%s'", file_path)
        raise


def save_json_file(data, file_path: str):
    """
    Save a Python object as a JSON file.

    Parameters
    ----------
    data : dict or list
        The Python object to be saved as JSON.
    file_path : str
        The path where the JSON file will be saved.

    Raises
    ------
    IOError
        If there is an error writing to the file.
    """
    logger.debug("Saving json file: %s", file_path)
    try:
        with open(file_path, "w", encoding="utf-8") as file:
            json.dump(data, file, ensure_ascii=False, indent=4)
    except IOError as e:
        logger.error("Error writing to file '%s': %s", file_path, e)
        raise


def anonymize_json_data(
    input_json: list | dict,
    anonymizer: utils.SeededNameAnonymizer,
    categories: Optional[List[str]] = None,
):
    """
    Anonymize the content of a JSON object based on specified categories.

    Parameters
    ----------
    input_json : dict or list
        The Python object to be anonymized.
    anonymizer : SeededNameAnonymizer
        The anonymizer to use for anonymizing the data.
    categories : Optional[List[str]]
        The list of categories to anonymize.

    Returns
    -------
    dict or list
        The anonymized JSON object.
    """
    if isinstance(input_json, dict):
        input_json = _anonymize_json_dict(input_json, anonymizer, categories)
    elif isinstance(input_json, list):
        input_json = _anonymize_json_list(input_json, anonymizer, categories)

    return input_json


def _anonymize_json_dict(input_dict: dict, anonymizer, categories):
    for key, element in input_dict.items():
        if isinstance(element, dict):
            anonymized_value = _anonymize_json_dict(element, anonymizer, categories)
        elif isinstance(element, list):
            anonymized_value = _anonymize_json_list(element, anonymizer, categories)
        elif key in categories:
            anonymized_value = anonymizer.translate(element)
        else:
            anonymized_value = element
        input_dict[key] = anonymized_value
    return input_dict


def _anonymize_json_list(input_list, anonymizer, categories):
    for entry in input_list:
        if isinstance(entry, dict):
            entry = _anonymize_json_dict(entry, anonymizer, categories)
        elif isinstance(entry, list):
            entry = _anonymize_json_list(entry, anonymizer, categories)
    return input_list


def _get_json_keys(data: list | dict) -> List[str]:
    all_keys = set()
    if isinstance(data, list):
        # Über alle Einträge in der Liste iterieren
        for entry in data:
            if isinstance(
                entry, dict
            ):  # Sicherstellen, dass es sich um ein Dictionary handelt
                all_keys.update(entry.keys())

    elif isinstance(data, dict):
        all_keys.update(data.keys())

    # Das Set in eine sortierte Liste umwandeln (für bessere Lesbarkeit)
    unique_keys_list = sorted(list(all_keys))

    return unique_keys_list


def anonymize_json_file(
    input_json: str,
    output_json: str,
    mapping_output: Path,
    anonymizer: utils.SeededNameAnonymizer,
    categories: Optional[List[str]] = None,
):
    """
    Anonymize the content of a JSON file and save the result to another file.

    Parameters
    ----------
    input_json : str
        The path to the input JSON file to be anonymized.
    output_json : str
        The path where the anonymized JSON will be saved.
    mapping_output : Path
        The path where the mapping file for anonymization will be saved.
    seed : str
        The seed for reproducible anonymization.
    categories : Optional[List[str]]
        The list of categories to anonymize.

    Raises
    ------
    FileNotFoundError
        If the input file does not exist.
    json.JSONDecodeError
        If the input file is not a valid JSON.
    """
    data = load_json_file(input_json)

    logger.info("Starting Anonymization of JSON Data")
    if categories:
        prefered_categories = categories
    else:
        prefered_categories = _get_json_keys(data)

    anonymized_data = anonymize_json_data(data, anonymizer, prefered_categories)

    utils.save_mapping_json(mapping_output, anonymizer)
    save_json_file(anonymized_data, output_json)

    logger.info("Anonymization finished!")


def restore_json_anonymization(
    input_json: str,
    output_json: str,
    mapping_input: Path,
):
    """
    Reconstruct the original content of an anonymized JSON file using a mapping.

    Parameters
    ----------
    input_json : str
        The path to the anonymized JSON file.
    output_json : str
        The path where the reconstructed JSON will be saved.
    mapping_input : Path
        The path to the mapping file used for reconstruction.
    categories : Optional[List[str]]
        The list of categories to reconstruct.

    Raises
    ------
    FileNotFoundError
        If the input file or mapping file does not exist.
    json.JSONDecodeError
        If the input file is not a valid JSON.
    """
    logger.info("Starting Restoration of JSON Data")

    data = load_json_file(input_json)
    (
        _,
        anon_rev,
        _,
        _,
        _,
        _,
        prefix,
    ) = utils.get_mappings(mapping_input)

    if isinstance(data, dict):
        data = _restore_json_dict(data, anon_rev, prefix)
    elif isinstance(data, list):
        data = _restore_json_list(data, anon_rev, prefix)

    save_json_file(data, output_json)

    logger.info("Restoration finished!")


def _restore_json_list(data, anon_rev, prefix):
    for entry in data:
        if isinstance(entry, dict):
            entry = _restore_json_dict(entry, anon_rev, prefix)
        elif isinstance(entry, list):
            entry = _restore_json_list(entry, anon_rev, prefix)
    return data


def _restore_json_dict(data, anon_rev, prefix):
    for key, value in data.items():
        if isinstance(value, dict):
            value = _restore_json_dict(value, anon_rev, prefix)
            continue
        if isinstance(value, list):
            value = _restore_json_list(value, anon_rev, prefix)
            continue
        if isinstance(value, str) and value.startswith(prefix):
            original_value = anon_rev.get(value)
            if original_value:
                data[key] = original_value
    return data
