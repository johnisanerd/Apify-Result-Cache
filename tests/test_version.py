"""One version number, everywhere. Closes the limiter's v0.1.8 slip, where
`__version__` and the README tarball pin were left behind by the release."""

from __future__ import annotations

import pathlib
import re
import tomllib

from apify_result_cache import __version__

ROOT = pathlib.Path(__file__).resolve().parents[1]
PIN = re.compile(r"Apify-Result-Cache/archive/refs/tags/v(\d+\.\d+\.\d+)\.tar\.gz")


def test_versions_agree():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
    assert __version__ == project

    for doc in ("README.md", "INSTALL.md"):
        pins = set(PIN.findall((ROOT / doc).read_text()))
        assert pins == {project}, f"{doc} pins {pins}, expected {{'{project}'}}"
