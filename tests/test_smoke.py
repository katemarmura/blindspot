"""Verifies the package installs and imports cleanly."""

import blindspot


def test_package_imports() -> None:
    assert blindspot.__doc__
