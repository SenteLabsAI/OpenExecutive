"""Guards the API image's dependency layer in ``docker/Dockerfile``.

That layer COPYs only ``pyproject.toml`` and ``uv.lock`` into the image and
must install exactly the versions the lockfile pins (#87) without needing
``README.md`` (#81 — any extra file COPYed before the install re-runs the
full dependency install and the model bakes whenever it changes). CI never
builds the image (it is built at deploy time), so this file guards the
layer's shape and behaviour. Lock *freshness* is guarded separately by the
``uv lock --check`` step in ``.github/workflows/ci.yml``: ``uv sync`` would
refresh a stale lock in the checkout before these tests ever ran.

Rather than pattern-matching the Dockerfile, the tests parse the stage's
``COPY`` lines and the export ``RUN``, run the export command *taken from*
the Dockerfile inside a directory holding only the COPYed files, and check
that the install half consumes that export.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tomllib
from pathlib import Path
from typing import NamedTuple

import pytest

REPO_ROOT = Path(__file__).resolve().parents[4]
DOCKERFILE = REPO_ROOT / "docker" / "Dockerfile"
CORE_DIR = REPO_ROOT / "packages" / "core"
# Everything the build stage may COPY before the dependency install (paths as
# written in the Dockerfile; the build context is the repo root). README.md is
# deliberately absent (#81).
DEPENDENCY_LAYER_SOURCES = frozenset({"packages/core/pyproject.toml", "packages/core/uv.lock"})
EXPORT_OUTPUT_FLAGS = ("-o", "--output-file")
# `name==version` with an optional environment marker (`; sys_platform == 'linux'`).
PIN_LINE = re.compile(r"^([A-Za-z0-9_.-]+)==(\S+)(?:\s*;.*)?$")


class DependencyLayer(NamedTuple):
    copied: frozenset[str]  # COPY/ADD sources before the export, as written
    export: list[str]  # the `uv export ...` argv
    install: list[str]  # the `uv pip install ...` argv


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _normalize(name: str) -> str:
    """PEP 503 name normalisation (``discord.py`` -> ``discord-py``)."""
    return re.sub(r"[-_.]+", "-", name).lower()


def _requirement_name(spec: str) -> str:
    return _normalize(re.split(r"[\s<>=!~\[;]", spec, maxsplit=1)[0])


def _dockerfile_instructions() -> list[str]:
    """Instructions as Docker sees them: comment lines dropped, continuations joined."""
    kept = [line for line in _read(DOCKERFILE).splitlines() if not line.strip().startswith("#")]
    joined = "\n".join(kept).replace("\\\n", " ")
    return [line.strip() for line in joined.splitlines() if line.strip()]


def _positional_args(instruction: str) -> list[str]:
    """COPY/ADD arguments with option flags (``--chown``, ``--from``...) removed."""
    return [token for token in instruction.split()[1:] if not token.startswith("--")]


def _dependency_layer() -> DependencyLayer:
    instructions = _dockerfile_instructions()
    exports = [
        (index, match)
        for index, instruction in enumerate(instructions)
        if (match := re.fullmatch(r"RUN(?: --\S+)* (uv export .*)", instruction))
    ]
    if not exports:
        pytest.fail("docker/Dockerfile no longer exports uv.lock before installing (#87)")
    index, match = exports[0]
    stage_start = max(
        (
            i
            for i, instruction in enumerate(instructions[:index])
            if instruction.startswith("FROM ")
        ),
        default=0,
    )
    copied: set[str] = set()
    for instruction in instructions[stage_start:index]:
        if instruction.startswith(("COPY ", "ADD ")):
            copied.update(_positional_args(instruction)[:-1])  # last argument is the destination
    clauses = [clause.split() for clause in match.group(1).split("&&")]
    installs = [clause for clause in clauses if clause[:3] == ["uv", "pip", "install"]]
    assert installs, f"no `uv pip install` follows the export: {match.group(1)}"
    return DependencyLayer(frozenset(copied), clauses[0], installs[0])


def _export_output(export: list[str]) -> str:
    for flag in EXPORT_OUTPUT_FLAGS:
        if flag in export and export.index(flag) + 1 < len(export):
            return export[export.index(flag) + 1]
    pytest.fail(f"export command has no output file ({'/'.join(EXPORT_OUTPUT_FLAGS)}): {export}")


def _require_uv() -> None:
    if shutil.which("uv"):
        return
    if os.environ.get("CI"):
        pytest.fail("uv must be on PATH in CI so the dependency-layer guard actually runs")
    pytest.skip("uv is not on PATH")


def _materialize_layer(layer: DependencyLayer, directory: Path) -> None:
    """Copy exactly what the Dockerfile COPYs (flattened into one directory, like ``./``)."""
    for source in layer.copied:
        shutil.copy(REPO_ROOT / source, directory / Path(source).name)


@pytest.fixture(scope="module")
def exported(tmp_path_factory: pytest.TempPathFactory) -> str:
    """Run the Dockerfile's export in a layer holding only the COPYed files."""
    _require_uv()
    layer = _dependency_layer()
    directory = tmp_path_factory.mktemp("dependency-layer")
    _materialize_layer(layer, directory)
    result = subprocess.run(
        layer.export, cwd=directory, capture_output=True, text=True, timeout=120
    )
    assert result.returncode == 0, (
        f"{' '.join(layer.export)} failed in a layer containing only "
        f"{sorted(layer.copied)}:\n{result.stderr}"
    )
    return _read(directory / _export_output(layer.export))


def _requirement_blocks(exported: str) -> list[tuple[str, list[str]]]:
    """Each top-level requirement line with its indented continuation lines (hashes)."""
    blocks: list[tuple[str, list[str]]] = []
    for line in exported.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if line[0].isspace():
            if blocks:
                blocks[-1][1].append(line.strip().rstrip("\\").strip())
        else:
            blocks.append((line.rstrip().rstrip("\\").strip(), []))
    return blocks


def _pins(exported: str) -> dict[str, set[str]]:
    """name -> versions pinned with ``==`` (one name can pin per-marker versions)."""
    pins: dict[str, set[str]] = {}
    for head, _continuation in _requirement_blocks(exported):
        if match := PIN_LINE.match(head):
            pins.setdefault(_normalize(match.group(1)), set()).add(match.group(2))
    return pins


def _runtime_closure() -> set[str]:
    """Names reachable from the project's runtime dependencies in uv.lock."""
    lock = tomllib.loads(_read(CORE_DIR / "uv.lock"))
    edges: dict[str, set[str]] = {}
    for package in lock["package"]:
        edges.setdefault(_normalize(package["name"]), set()).update(
            _normalize(dep["name"]) for dep in package.get("dependencies", [])
        )
    project = _normalize(tomllib.loads(_read(CORE_DIR / "pyproject.toml"))["project"]["name"])
    closure: set[str] = set()
    stack = list(edges.get(project, set()))
    while stack:
        name = stack.pop()
        if name not in closure:
            closure.add(name)
            stack.extend(edges.get(name, ()))
    return closure


def test_stage_copies_only_the_lock_inputs_before_installing() -> None:
    copied = _dependency_layer().copied
    assert copied == DEPENDENCY_LAYER_SOURCES, (
        f"files COPYed before the dependency install: {sorted(copied)}; anything beyond "
        f"{sorted(DEPENDENCY_LAYER_SOURCES)} re-runs the install and model bakes on every edit (#81)"
    )


def test_export_is_locked_not_frozen() -> None:
    """``--locked`` fails on a stale lock; ``--frozen`` silently uses it as-is."""
    export = _dependency_layer().export
    assert "--locked" in export, f"export must assert uv.lock is current: {export}"
    assert "--frozen" not in export, "--frozen skips the staleness check --locked provides"


def test_install_consumes_the_export_not_pyproject() -> None:
    layer = _dependency_layer()
    requirements = _export_output(layer.export)
    assert "--system" in layer.install, "later layers and the CMD rely on system site-packages"
    assert "pyproject.toml" not in layer.install, (
        "installing from pyproject re-resolves floating versions (#87)"
    )
    assert requirements in layer.install, f"install must read the exported {requirements!r}"
    assert layer.install[layer.install.index(requirements) - 1] == "-r", layer.install


def test_stale_lock_fails_the_export(tmp_path: Path) -> None:
    """A pyproject edit without ``uv lock`` must break the build, not ship silently."""
    _require_uv()
    layer = _dependency_layer()
    _materialize_layer(layer, tmp_path)
    pyproject = tmp_path / "pyproject.toml"
    stale = _read(pyproject).replace(
        "dependencies = [", 'dependencies = [\n    "stripe>=10.0.0",', 1
    )
    pyproject.write_text(stale, encoding="utf-8")
    result = subprocess.run(layer.export, cwd=tmp_path, capture_output=True, text=True, timeout=120)
    assert result.returncode != 0, (
        "export accepted a lockfile that no longer matches pyproject.toml"
    )
    assert "needs to be updated" in result.stderr, result.stderr


def test_every_requirement_is_an_exact_pin_with_hashes(exported: str) -> None:
    blocks = _requirement_blocks(exported)
    assert blocks, "export produced no requirements"
    not_pinned = [head for head, _ in blocks if not PIN_LINE.match(head)]
    assert not not_pinned, f"requirements that are not exact `name==version` pins: {not_pinned}"
    unhashed = [
        head
        for head, continuation in blocks
        if not any(part.startswith("--hash=sha256:") for part in continuation)
    ]
    assert not unhashed, (
        f"requirements without hashes (uv pip install can't verify them): {unhashed}"
    )
    pins = _pins(exported)
    runtime = tomllib.loads(_read(CORE_DIR / "pyproject.toml"))["project"]["dependencies"]
    missing = [name for name in map(_requirement_name, runtime) if name not in pins]
    assert not missing, f"runtime dependencies missing from the image install: {missing}"


def test_layer_excludes_dev_tooling_and_the_project_itself(exported: str) -> None:
    pyproject = tomllib.loads(_read(CORE_DIR / "pyproject.toml"))
    dev_specs = pyproject.get("dependency-groups", {}).get("dev", []) + pyproject["project"].get(
        "optional-dependencies", {}
    ).get("dev", [])
    assert dev_specs, "no dev dependency list found; update this test if dev tooling moved"
    dev_only = {_requirement_name(spec) for spec in dev_specs} - _runtime_closure()
    leaked = sorted(dev_only & _pins(exported).keys())
    assert not leaked, f"dev-only packages would ship in the image: {leaked}"
    project_lines = [
        head
        for head, _ in _requirement_blocks(exported)
        if re.match(r"^(-e\b|\.|openexecutive\b)", head)
    ]
    assert not project_lines, (
        f"the project itself leaked into the dependency layer {project_lines}; it is installed "
        "in a later layer (--no-deps .) and building it here would need README.md (#81)"
    )
