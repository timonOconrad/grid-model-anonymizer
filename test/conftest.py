"""Pytest configuration: adds a --skip-slow option to skip tests marked as slow."""

import pytest


def pytest_addoption(parser):
    """Register the --skip-slow command-line option."""
    parser.addoption(
        "--skip-slow",
        action="store_true",
        default=False,
        help="Skip Tests, that are marked with @pytest.mark.slow",
    )


def pytest_configure(config):
    """Register the custom 'slow' marker with pytest."""
    config.addinivalue_line("markers", "slow: marks test as slow")


def pytest_collection_modifyitems(config, items):
    """Skip tests marked 'slow' when --skip-slow is passed."""
    if not config.getoption("--skip-slow"):
        return
    skip_marker = pytest.mark.skip(reason="skip slow")
    for item in items:
        if "slow" in item.keywords:
            item.add_marker(skip_marker)
