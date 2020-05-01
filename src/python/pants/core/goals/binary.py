# Copyright 2019 Pants project contributors (see CONTRIBUTORS.md).
# Licensed under the Apache License, Version 2.0 (see LICENSE).

import os
from abc import ABCMeta
from dataclasses import dataclass

from pants.base.build_root import BuildRoot
from pants.core.util_rules.distdir import DistDir
from pants.engine.console import Console
from pants.engine.fs import Digest, DirectoryToMaterialize, MergeDigests, Workspace
from pants.engine.goal import Goal, GoalSubsystem, LineOriented
from pants.engine.rules import goal_rule
from pants.engine.selectors import Get, MultiGet
from pants.engine.target import FieldSet, TargetsToValidFieldSets, TargetsToValidFieldSetsRequest
from pants.engine.unions import union


@union
class BinaryFieldSet(FieldSet, metaclass=ABCMeta):
    """The fields necessary to create a binary from a target."""


@union
@dataclass(frozen=True)
class CreatedBinary:
    digest: Digest
    binary_name: str


class BinaryOptions(LineOriented, GoalSubsystem):
    """Create a runnable binary."""

    name = "binary"

    required_union_implementations = (BinaryFieldSet,)


class Binary(Goal):
    subsystem_cls = BinaryOptions


@goal_rule
async def create_binary(
    console: Console,
    workspace: Workspace,
    options: BinaryOptions,
    distdir: DistDir,
    buildroot: BuildRoot,
) -> Binary:
    targets_to_valid_field_sets = await Get[TargetsToValidFieldSets](
        TargetsToValidFieldSetsRequest(
            BinaryFieldSet,
            goal_description=f"the `{options.name}` goal",
            error_if_no_valid_targets=True,
        )
    )
    binaries = await MultiGet(
        Get[CreatedBinary](BinaryFieldSet, field_set)
        for field_set in targets_to_valid_field_sets.field_sets
    )
    merged_digest = await Get[Digest](MergeDigests(binary.digest for binary in binaries))
    result = workspace.materialize_directory(
        DirectoryToMaterialize(merged_digest, path_prefix=str(distdir.relpath))
    )
    with options.line_oriented(console) as print_stdout:
        for path in result.output_paths:
            print_stdout(f"Wrote {os.path.relpath(path, buildroot.path)}")
    return Binary(exit_code=0)


def rules():
    return [create_binary]