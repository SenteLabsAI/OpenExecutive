"""Guards the API image's dependency layer in ``docker/Dockerfile``.

That layer COPYs only ``pyproject.toml`` and ``uv.lock`` into the image and
must install exactly the versions the lockfile pins (#87) without needing
``README.md`` (#81 — any extra file COPYed before the install re-runs the
full dependency install and the model bakes whenever it changes). CI never
builds the image (it is built by the publish workflow after merge), so this
file guards the layer's shape and behaviour. Lock *freshness* is guarded
separately by the ``uv lock --check`` step in ``.github/workflows/ci.yml``:
``uv sync`` would refresh a stale lock in the checkout before these tests
ever ran.

The layer installs straight from the lock with ``uv sync`` so that each
package comes from the index the lock names — torch from PyTorch's CPU-only
index, everything else from PyPI (see ``[tool.uv.sources]``). The earlier
``uv export`` + ``uv pip install -r`` shape could not express that: the
export carries no index URLs, and adding the CPU index at install time made
uv fetch every package that mirror also hosts from it instead of PyPI.

Rather than pattern-matching the Dockerfile, the tests parse the stage's
``COPY`` lines and the sync ``RUN``, then run the sync command *taken from*
the Dockerfile — with ``--dry-run`` and the environment redirected to a temp
dir — inside a directory holding only the COPYed files, and check the
install plan it prints.
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
# Where the image's interpreter lives; the sync must land in its site-packages
# because the model bakes and the runtime CMD rely on system site-packages.
SYSTEM_PREFIX = "/usr/local"
ENV_ASSIGNMENT = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(\S*)$")
# `name==version` as `uv sync --dry-run` plans it (`torch==2.14.0+cpu`).
CPU_TORCH_INDEX = "https://download.pytorch.org/whl/cpu"
PLAN_LINE = re.compile(r"^\s*\+\s+([A-Za-z0-9_.-]+)==(\S+)$")


class DependencyLayer(NamedTuple):
    copied: frozenset[str]  # COPY/ADD sources before the sync, as written
    env: dict[str, str]  # KEY=value assignments prefixed to the sync command
    sync: list[str]  # the `uv sync ...` argv


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
    syncs = [
        (index, match)
        for index, instruction in enumerate(instructions)
        if (match := re.fullmatch(r"RUN(?: --\S+)*\s+((?:\S+=\S*\s+)*uv sync\b.*)", instruction))
    ]
    if not syncs:
        pytest.fail(
            "docker/Dockerfile no longer installs the dependency layer with `uv sync` (#87)"
        )
    index, match = syncs[0]
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
    first_clause = match.group(1).split("&&")[0].split()
    env: dict[str, str] = {}
    while first_clause and (assignment := ENV_ASSIGNMENT.match(first_clause[0])):
        env[assignment.group(1)] = assignment.group(2)
        first_clause.pop(0)
    assert first_clause[:2] == ["uv", "sync"], first_clause
    return DependencyLayer(frozenset(copied), env, first_clause)


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


def _dry_run(layer: DependencyLayer, cwd: Path) -> subprocess.CompletedProcess[str]:
    """Run the Dockerfile's sync as a dry run, with its environment redirected away from /usr/local."""
    env = {**os.environ, **layer.env, "UV_PROJECT_ENVIRONMENT": str(cwd / "planned-env")}
    return subprocess.run(
        [*layer.sync, "--dry-run"], cwd=cwd, env=env, capture_output=True, text=True, timeout=120
    )


@pytest.fixture(scope="module")
def planned(tmp_path_factory: pytest.TempPathFactory) -> dict[str, str]:
    """name -> version the Dockerfile's sync would install, from a layer holding only the COPYed files."""
    _require_uv()
    layer = _dependency_layer()
    directory = tmp_path_factory.mktemp("dependency-layer")
    _materialize_layer(layer, directory)
    result = _dry_run(layer, directory)
    assert result.returncode == 0, (
        f"{' '.join(layer.sync)} failed in a layer containing only "
        f"{sorted(layer.copied)}:\n{result.stderr}"
    )
    plan = {
        _normalize(match.group(1)): match.group(2)
        for line in result.stderr.splitlines()
        if (match := PLAN_LINE.match(line))
    }
    assert plan, f"dry run planned no installs:\n{result.stderr}"
    return plan


def _lock() -> dict:
    return tomllib.loads(_read(CORE_DIR / "uv.lock"))


def _runtime_closure() -> set[str]:
    """Names reachable from the project's runtime dependencies in uv.lock."""
    edges: dict[str, set[str]] = {}
    for package in _lock()["package"]:
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


def test_sync_is_locked_not_frozen() -> None:
    """``--locked`` fails on a stale lock; ``--frozen`` silently uses it as-is."""
    sync = _dependency_layer().sync
    assert "--locked" in sync, f"sync must assert uv.lock is current: {sync}"
    assert "--frozen" not in sync, "--frozen skips the staleness check --locked provides"


def test_sync_targets_system_site_packages_and_only_the_runtime_deps() -> None:
    layer = _dependency_layer()
    assert layer.env.get("UV_PROJECT_ENVIRONMENT") == SYSTEM_PREFIX, (
        f"later layers and the CMD rely on system site-packages under {SYSTEM_PREFIX}: {layer.env}"
    )
    assert "--no-dev" in layer.sync, "dev tooling must not ship in the image"
    assert "--no-install-project" in layer.sync, (
        "the project is installed in a later layer (--no-deps .); building it here needs README.md (#81)"
    )
    assert "--inexact" in layer.sync, (
        "an exact sync removes packages the lock does not know about — uv itself and the fastembed warm-up"
    )
    index_flags = [arg for arg in layer.sync if "index" in arg]
    assert not index_flags, (
        f"index flags on the install override the lock's per-package index; an extra index outranks "
        f"PyPI and pulls every package it mirrors from there instead: {index_flags}"
    )


def test_stale_lock_fails_the_sync(tmp_path: Path) -> None:
    """A pyproject edit without ``uv lock`` must break the build, not ship silently."""
    _require_uv()
    layer = _dependency_layer()
    _materialize_layer(layer, tmp_path)
    pyproject = tmp_path / "pyproject.toml"
    stale = _read(pyproject).replace(
        "dependencies = [", 'dependencies = [\n    "stripe>=10.0.0",', 1
    )
    pyproject.write_text(stale, encoding="utf-8")
    result = _dry_run(layer, tmp_path)
    assert result.returncode != 0, "sync accepted a lockfile that no longer matches pyproject.toml"
    assert "needs to be updated" in result.stderr, result.stderr


def test_plan_pins_every_runtime_dependency_with_a_hashed_lock_entry(
    planned: dict[str, str],
) -> None:
    runtime = tomllib.loads(_read(CORE_DIR / "pyproject.toml"))["project"]["dependencies"]
    missing = [name for name in map(_requirement_name, runtime) if name not in planned]
    assert not missing, f"runtime dependencies missing from the image install: {missing}"
    # `uv sync --locked` verifies downloads against the lock's hashes, so every
    # planned package must carry at least one hashed artifact in uv.lock.
    hashed = {
        _normalize(package["name"])
        for package in _lock()["package"]
        if any("hash" in artifact for artifact in package.get("wheels", []))
        or "hash" in package.get("sdist", {})
    }
    unhashed = sorted(name for name in planned if name not in hashed)
    assert not unhashed, (
        f"planned packages with no hash in uv.lock (install cannot verify them): {unhashed}"
    )


def test_plan_installs_the_cpu_only_torch_build(planned: dict[str, str]) -> None:
    """The container runs on CPU hosts; the CUDA build is ~2.2 GB of wheels it never uses.

    The dry run resolves for the machine running the test, so the planned torch
    version differs by host: ``2.14.0+cpu`` on Linux (CI, the image), plain
    ``2.14.0`` on macOS, where PyTorch's CPU index publishes no ``+cpu`` tag.
    Asserting ``+cpu`` on the plan failed every Mac run. What must hold on every
    host is that torch resolves from the CPU-only index, and that the Linux
    entry — the one the image installs — is the ``+cpu`` build.
    """
    assert "torch" in planned, sorted(planned)
    torch_entries = [p for p in _lock()["package"] if _normalize(p["name"]) == "torch"]
    assert torch_entries, "torch is missing from uv.lock"
    not_cpu_index = sorted(
        entry["version"]
        for entry in torch_entries
        if entry.get("source", {}).get("registry") != CPU_TORCH_INDEX
    )
    assert not not_cpu_index, (
        f"torch {not_cpu_index} resolves from outside {CPU_TORCH_INDEX}; check [tool.uv.sources] in "
        "pyproject.toml and that torch is still a direct dependency (uv ignores sources for "
        "transitive packages)"
    )
    assert any(entry["version"].endswith("+cpu") for entry in torch_entries), (
        f"no +cpu torch build in uv.lock for Linux: {[e['version'] for e in torch_entries]}"
    )
    assert planned["torch"] in {entry["version"] for entry in torch_entries}, planned["torch"]
    gpu_only = sorted(name for name in planned if name.startswith("nvidia-") or name in {"triton"})
    assert not gpu_only, f"GPU-only packages would ship in the image: {gpu_only}"


def test_plan_excludes_dev_tooling_and_the_project_itself(planned: dict[str, str]) -> None:
    pyproject = tomllib.loads(_read(CORE_DIR / "pyproject.toml"))
    dev_specs = pyproject.get("dependency-groups", {}).get("dev", []) + pyproject["project"].get(
        "optional-dependencies", {}
    ).get("dev", [])
    assert dev_specs, "no dev dependency list found; update this test if dev tooling moved"
    dev_only = {_requirement_name(spec) for spec in dev_specs} - _runtime_closure()
    leaked = sorted(dev_only & planned.keys())
    assert not leaked, f"dev-only packages would ship in the image: {leaked}"
    project = _normalize(pyproject["project"]["name"])
    assert project not in planned, (
        "the project itself leaked into the dependency layer; it is installed in a later layer "
        "(--no-deps .) and building it here would need README.md (#81)"
    )
