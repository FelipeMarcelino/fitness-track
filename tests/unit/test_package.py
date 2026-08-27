"""The package must be importable and expose its version from the distribution."""

from __future__ import annotations

import importlib.metadata

import fittrack


def test_package_is_importable() -> None:
    assert fittrack.__doc__


def test_version_matches_installed_distribution() -> None:
    assert fittrack.__version__ == importlib.metadata.version("fittrack")


def test_requires_python_313() -> None:
    import sys

    assert sys.version_info[:2] == (3, 13)
