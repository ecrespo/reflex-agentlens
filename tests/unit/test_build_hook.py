"""The wheel build hook and the frontend build helpers it calls (plan task P7-01.1; REQ-200.b,
DD-002, DD-018, DD-022). The export itself needs Bun and runs in the CI ``wheel`` job (P7-01.2)."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType

import pytest

from reflex_agentlens._webapp.build import (
    CSP_FILE,
    bundle_script_hashes,
    inline_script_hashes,
    read_csp,
    write_csp,
)

ROOT = Path(__file__).resolve().parents[2]


def hook_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("hatch_build_under_test", ROOT / "hatch_build.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def bundle(tmp_path: Path) -> Path:
    client = tmp_path / "client"
    (client / "runs").mkdir(parents=True)
    (client / "index.html").write_text("<script>a()</script><script src='/x.js'></script>")
    (client / "runs" / "index.html").write_text("<script>b()</script><script>a()</script>")
    return client


def test_write_csp_hashes_every_page_once_dd018(tmp_path: Path) -> None:
    client = bundle(tmp_path)
    hashes = write_csp(client)
    expected = tuple(sorted(inline_script_hashes("<script>a()</script><script>b()</script>")))
    assert hashes == expected
    assert bundle_script_hashes(client) == expected
    assert json.loads((client / CSP_FILE).read_text()) == {"script_hashes": list(expected)}
    assert read_csp(client) == expected


@pytest.mark.parametrize(
    "content",
    [
        None,
        "not json",
        "[]",
        '{"script_hashes": "x"}',
        '{"script_hashes": ["\'unsafe-inline\'"]}',
        '{"script_hashes": [1]}',
    ],
)
def test_read_csp_rejects_missing_or_malformed_files(tmp_path: Path, content: str | None) -> None:
    if content is not None:
        (tmp_path / CSP_FILE).write_text(content)
    assert read_csp(tmp_path) is None


@pytest.mark.parametrize(
    ("target", "version", "env", "expected"),
    [
        ("wheel", "standard", {}, True),
        ("wheel", "editable", {}, False),  # uv sync needs no JS runtime
        ("sdist", "standard", {}, False),
        ("wheel", "standard", {"AGENTLENS_SKIP_FRONTEND": "1"}, False),
        ("wheel", "standard", {"AGENTLENS_SKIP_FRONTEND": "0"}, True),
    ],
)
def test_hook_exports_only_for_standard_wheels_req200b(
    target: str, version: str, env: dict[str, str], expected: bool
) -> None:
    assert hook_module().should_export(target, version, env) is expected


def test_hook_loads_the_build_module_without_the_package() -> None:
    hook = hook_module()
    build = hook.load_build_module(ROOT)
    assert build.__name__ == "_agentlens_build"
    assert callable(build.export)
    assert callable(build.write_csp)
    # the hook runs in an isolated env: build.py may import only the standard library
    source = (ROOT / hook.BUILD_MODULE).read_text()
    imports = [line for line in source.splitlines() if line.startswith(("import ", "from "))]
    allowed = {"__future__", "argparse", "base64", "hashlib", "json", "os", "re", "shutil",
               "subprocess", "sys", "collections.abc", "pathlib"}  # fmt: skip
    assert {line.split()[1] for line in imports} <= allowed
