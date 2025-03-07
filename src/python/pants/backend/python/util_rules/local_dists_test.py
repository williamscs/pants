# Copyright 2021 Pants project contributors (see CONTRIBUTORS.md).
# Licensed under the Apache License, Version 2.0 (see LICENSE).

from __future__ import annotations

import io
import zipfile
from pathlib import PurePath
from textwrap import dedent

import pytest

from pants.backend.python import target_types_rules
from pants.backend.python.goals.setup_py import rules as setup_py_rules
from pants.backend.python.macros.python_artifact import PythonArtifact
from pants.backend.python.subsystems.setuptools import rules as setuptools_rules
from pants.backend.python.target_types import PythonDistribution, PythonLibrary
from pants.backend.python.util_rules import local_dists
from pants.backend.python.util_rules.local_dists import LocalDistsPex, LocalDistsPexRequest
from pants.backend.python.util_rules.python_sources import PythonSourceFiles
from pants.build_graph.address import Address
from pants.core.util_rules.source_files import SourceFiles
from pants.engine.fs import CreateDigest, Digest, DigestContents, FileContent, Snapshot
from pants.testutil.rule_runner import QueryRule, RuleRunner


@pytest.fixture
def rule_runner() -> RuleRunner:
    return RuleRunner(
        rules=[
            *local_dists.rules(),
            *setup_py_rules(),
            *setuptools_rules(),
            *target_types_rules.rules(),
            QueryRule(LocalDistsPex, (LocalDistsPexRequest,)),
        ],
        target_types=[PythonLibrary, PythonDistribution],
        objects={"python_artifact": PythonArtifact},
    )


def test_build_local_dists(rule_runner: RuleRunner) -> None:
    foo = PurePath("foo")
    rule_runner.write_files(
        {
            foo
            / "BUILD": dedent(
                """
            python_library()

            python_distribution(
                name = "dist",
                dependencies = [":foo"],
                provides = python_artifact(name="foo", version="9.8.7", setup_script="setup.py"),
                setup_py_commands = ["bdist_wheel",]
            )
            """
            ),
            foo / "bar.py": "BAR = 42",
            foo
            / "setup.py": dedent(
                """
                from setuptools import setup

                setup(name="foo", version="9.8.7", packages=["foo"], package_dir={"foo": "."},)
                """
            ),
        }
    )
    rule_runner.set_options([], env_inherit={"PATH"})
    sources_digest = rule_runner.request(
        Digest,
        [
            CreateDigest(
                [FileContent("srcroot/foo/bar.py", b""), FileContent("srcroot/foo/qux.py", b"")]
            )
        ],
    )
    sources_snapshot = rule_runner.request(Snapshot, [sources_digest])
    sources = PythonSourceFiles(SourceFiles(sources_snapshot, tuple()), ("srcroot",))
    request = LocalDistsPexRequest([Address("foo", target_name="dist")], sources=sources)
    result = rule_runner.request(LocalDistsPex, [request])

    assert result.pex is not None
    contents = rule_runner.request(DigestContents, [result.pex.digest])
    whl_content = None
    for content in contents:
        if content.path == "local_dists.pex/.deps/foo-9.8.7-py3-none-any.whl":
            whl_content = content
    assert whl_content
    with io.BytesIO(whl_content.content) as fp:
        with zipfile.ZipFile(fp, "r") as whl:
            assert "foo/bar.py" in whl.namelist()

    # Check that srcroot/foo/bar.py was subtracted out, because the dist provides foo/bar.py.
    assert result.remaining_sources.source_files.files == ("srcroot/foo/qux.py",)
