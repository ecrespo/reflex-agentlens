"""Release version facts (plan task P7-05.1; REQ-206.a, Tech Design §9.1)."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

import reflex_agentlens

ROOT = Path(__file__).resolve().parents[2]


def _module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("release_plan", ROOT / "scripts/release_plan.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


rp = _module()


def test_reads_the_package_version_without_importing_it() -> None:
    assert rp.read_version() == reflex_agentlens.__version__


@pytest.mark.parametrize(
    ("version", "prerelease", "tags"),
    [
        ("0.1.0", False, ["0.1.0", "0.1", "latest"]),  # REQ-206.a
        ("1.12.3", False, ["1.12.3", "1.12", "latest"]),
        ("0.1.0a1", True, ["0.1.0-alpha.1"]),  # §9.1: :0.1.0-alpha.N
        ("0.1.0b2", True, ["0.1.0-beta.2"]),
        ("0.1.0rc1", True, ["0.1.0-rc.1"]),
        ("0.1.0.dev0", True, ["0.1.0-dev.0"]),
        ("0.1.0a1.dev3", True, ["0.1.0-alpha.1.dev.3"]),
    ],
)
def test_prerelease_and_image_tags_req206a(version: str, prerelease: bool, tags: list[str]) -> None:
    assert rp.is_prerelease(version) is prerelease
    assert rp.image_tags(version) == tags


@pytest.mark.parametrize("version", ["0.1", "v0.1.0", "0.1.0-alpha.1", "0.1.0post1", "latest"])
def test_rejects_unsupported_versions(version: str) -> None:
    with pytest.raises(ValueError, match="unsupported version"):
        rp.image_tags(version)


def test_cli(capsys: pytest.CaptureFixture[str]) -> None:
    assert rp.main(["image-tags", "0.2.0"]) == 0
    assert capsys.readouterr().out == "0.2.0 0.2 latest\n"
    assert rp.main(["prerelease", "0.2.0a1"]) == 0
    assert capsys.readouterr().out == "true\n"
    assert rp.main(["bogus"]) == 2
