# Copyright 2021 Pants project contributors (see CONTRIBUTORS.md).
# Licensed under the Apache License, Version 2.0 (see LICENSE).

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Iterable

from pants.backend.java.target_types import JavaLibrary, JavaTestsSources, JunitTests
from pants.core.goals.tailor import (
    AllOwnedSources,
    PutativeTarget,
    PutativeTargets,
    PutativeTargetsRequest,
    group_by_dir,
)
from pants.engine.fs import PathGlobs, Paths
from pants.engine.internals.selectors import Get
from pants.engine.rules import collect_rules, rule
from pants.engine.target import Target
from pants.engine.unions import UnionRule
from pants.source.filespec import Filespec, matches_filespec
from pants.util.logging import LogLevel


@dataclass(frozen=True)
class PutativeJavaTargetsRequest(PutativeTargetsRequest):
    pass


def classify_source_files(paths: Iterable[str]) -> dict[type[Target], set[str]]:
    """Returns a dict of target type -> files that belong to targets of that type."""
    tests_filespec = Filespec(includes=list(JavaTestsSources.default))
    test_filenames = set(
        matches_filespec(tests_filespec, paths=[os.path.basename(path) for path in paths])
    )
    test_files = {path for path in paths if os.path.basename(path) in test_filenames}
    library_files = set(paths) - test_files
    return {JunitTests: test_files, JavaLibrary: library_files}


@rule(level=LogLevel.DEBUG, desc="Determine candidate Java targets to create")
async def find_putative_targets(
    req: PutativeJavaTargetsRequest,
    all_owned_sources: AllOwnedSources,
) -> PutativeTargets:
    all_java_files_globs = req.search_paths.path_globs("*.java")
    all_java_files = await Get(Paths, PathGlobs, all_java_files_globs)
    unowned_java_files = set(all_java_files.files) - set(all_owned_sources)
    classified_unowned_java_files = classify_source_files(unowned_java_files)

    putative_targets = []
    for tgt_type, paths in classified_unowned_java_files.items():
        for dirname, filenames in group_by_dir(paths).items():
            name = "tests" if tgt_type == JunitTests else os.path.basename(dirname)
            kwargs = {"name": name} if tgt_type == JunitTests else {}
            putative_targets.append(
                PutativeTarget.for_target_type(
                    tgt_type, dirname, name, sorted(filenames), kwargs=kwargs
                )
            )

    return PutativeTargets(putative_targets)


def rules():
    return [
        *collect_rules(),
        UnionRule(PutativeTargetsRequest, PutativeJavaTargetsRequest),
    ]
