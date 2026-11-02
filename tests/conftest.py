"""Shared fixtures.

`REPO_ROOT` is resolved from this file's location rather than the working
directory, so the suite behaves the same from the repo root, from an IDE, and
in CI.
"""

from __future__ import annotations

import pathlib

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent


@pytest.fixture(scope="session")
def repo_root() -> pathlib.Path:
    return REPO_ROOT
