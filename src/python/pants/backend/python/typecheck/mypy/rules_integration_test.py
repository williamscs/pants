# Copyright 2020 Pants project contributors (see CONTRIBUTORS.md).
# Licensed under the Apache License, Version 2.0 (see LICENSE).

from __future__ import annotations

from textwrap import dedent

import pytest

from pants.backend.codegen.protobuf.python.python_protobuf_subsystem import (
    rules as protobuf_subsystem_rules,
)
from pants.backend.codegen.protobuf.python.rules import rules as protobuf_rules
from pants.backend.codegen.protobuf.target_types import ProtobufLibrary
from pants.backend.python import target_types_rules
from pants.backend.python.dependency_inference import rules as dependency_inference_rules
from pants.backend.python.target_types import PythonLibrary, PythonRequirementLibrary
from pants.backend.python.typecheck.mypy.rules import (
    MyPyFieldSet,
    MyPyRequest,
    determine_python_files,
)
from pants.backend.python.typecheck.mypy.rules import rules as mypy_rules
from pants.backend.python.typecheck.mypy.subsystem import MyPy
from pants.backend.python.typecheck.mypy.subsystem import rules as mypy_subystem_rules
from pants.core.goals.check import CheckResult, CheckResults
from pants.core.util_rules import config_files, pants_bin
from pants.engine.addresses import Address
from pants.engine.fs import EMPTY_DIGEST, DigestContents
from pants.engine.rules import QueryRule
from pants.engine.target import Target
from pants.testutil.python_interpreter_selection import (
    all_major_minor_python_versions,
    skip_unless_python27_and_python3_present,
    skip_unless_python27_present,
    skip_unless_python38_present,
    skip_unless_python39_present,
)
from pants.testutil.rule_runner import RuleRunner


@pytest.fixture
def rule_runner() -> RuleRunner:
    return RuleRunner(
        rules=[
            *mypy_rules(),
            *mypy_subystem_rules(),
            *dependency_inference_rules.rules(),  # Used for import inference.
            *pants_bin.rules(),
            *config_files.rules(),
            *target_types_rules.rules(),
            QueryRule(CheckResults, (MyPyRequest,)),
        ],
        target_types=[PythonLibrary, PythonRequirementLibrary],
    )


PACKAGE = "src/py/project"
GOOD_FILE = dedent(
    """\
    def add(x: int, y: int) -> int:
        return x + y

    result = add(3, 3)
    """
)
BAD_FILE = dedent(
    """\
    def add(x: int, y: int) -> int:
        return x + y

    result = add(2.0, 3.0)
    """
)
# This will fail if `--disallow-any-expr` is configured.
NEEDS_CONFIG_FILE = dedent(
    """\
    from typing import Any, cast

    x = cast(Any, "hello")
    """
)


def run_mypy(
    rule_runner: RuleRunner, targets: list[Target], *, extra_args: list[str] | None = None
) -> tuple[CheckResult, ...]:
    rule_runner.set_options(
        ["--backend-packages=pants.backend.python.typecheck.mypy", *(extra_args or ())],
        env_inherit={"PATH", "PYENV_ROOT", "HOME"},
    )
    result = rule_runner.request(
        CheckResults,
        [MyPyRequest(MyPyFieldSet.create(tgt) for tgt in targets)],
    )
    return result.results


def assert_success(
    rule_runner: RuleRunner, target: Target, *, extra_args: list[str] | None = None
) -> None:
    result = run_mypy(rule_runner, [target], extra_args=extra_args)
    assert len(result) == 1
    assert result[0].exit_code == 0
    assert "Success: no issues found" in result[0].stdout.strip()
    assert result[0].report == EMPTY_DIGEST


@pytest.mark.platform_specific_behavior
@pytest.mark.parametrize(
    "major_minor_interpreter",
    all_major_minor_python_versions(MyPy.default_interpreter_constraints),
)
def test_passing(rule_runner: RuleRunner, major_minor_interpreter: str) -> None:
    rule_runner.write_files({f"{PACKAGE}/f.py": GOOD_FILE, f"{PACKAGE}/BUILD": "python_library()"})
    tgt = rule_runner.get_target(Address(PACKAGE, relative_file_path="f.py"))
    assert_success(
        rule_runner,
        tgt,
        extra_args=[f"--mypy-interpreter-constraints=['=={major_minor_interpreter}.*']"],
    )


def test_failing(rule_runner: RuleRunner) -> None:
    rule_runner.write_files({f"{PACKAGE}/f.py": BAD_FILE, f"{PACKAGE}/BUILD": "python_library()"})
    tgt = rule_runner.get_target(Address(PACKAGE, relative_file_path="f.py"))
    result = run_mypy(rule_runner, [tgt])
    assert len(result) == 1
    assert result[0].exit_code == 1
    assert f"{PACKAGE}/f.py:4" in result[0].stdout
    assert result[0].report == EMPTY_DIGEST


def test_multiple_targets(rule_runner: RuleRunner) -> None:
    rule_runner.write_files(
        {
            f"{PACKAGE}/good.py": GOOD_FILE,
            f"{PACKAGE}/bad.py": BAD_FILE,
            f"{PACKAGE}/BUILD": "python_library()",
        }
    )
    tgts = [
        rule_runner.get_target(Address(PACKAGE, relative_file_path="good.py")),
        rule_runner.get_target(Address(PACKAGE, relative_file_path="bad.py")),
    ]
    result = run_mypy(rule_runner, tgts)
    assert len(result) == 1
    assert result[0].exit_code == 1
    assert f"{PACKAGE}/good.py" not in result[0].stdout
    assert f"{PACKAGE}/bad.py:4" in result[0].stdout
    assert "checked 2 source files" in result[0].stdout
    assert result[0].report == EMPTY_DIGEST


@pytest.mark.parametrize(
    "config_path,extra_args",
    ([".mypy.ini", []], ["custom_config.ini", ["--mypy-config=custom_config.ini"]]),
)
def test_config_file(rule_runner: RuleRunner, config_path: str, extra_args: list[str]) -> None:
    rule_runner.write_files(
        {
            f"{PACKAGE}/f.py": NEEDS_CONFIG_FILE,
            f"{PACKAGE}/BUILD": "python_library()",
            config_path: "[mypy]\ndisallow_any_expr = True\n",
        }
    )
    tgt = rule_runner.get_target(Address(PACKAGE, relative_file_path="f.py"))
    result = run_mypy(rule_runner, [tgt], extra_args=extra_args)
    assert len(result) == 1
    assert result[0].exit_code == 1
    assert f"{PACKAGE}/f.py:3" in result[0].stdout


def test_passthrough_args(rule_runner: RuleRunner) -> None:
    rule_runner.write_files(
        {f"{PACKAGE}/f.py": NEEDS_CONFIG_FILE, f"{PACKAGE}/BUILD": "python_library()"}
    )
    tgt = rule_runner.get_target(Address(PACKAGE, relative_file_path="f.py"))
    result = run_mypy(rule_runner, [tgt], extra_args=["--mypy-args='--disallow-any-expr'"])
    assert len(result) == 1
    assert result[0].exit_code == 1
    assert f"{PACKAGE}/f.py:3" in result[0].stdout


def test_skip(rule_runner: RuleRunner) -> None:
    rule_runner.write_files({f"{PACKAGE}/f.py": BAD_FILE, f"{PACKAGE}/BUILD": "python_library()"})
    tgt = rule_runner.get_target(Address(PACKAGE, relative_file_path="f.py"))
    result = run_mypy(rule_runner, [tgt], extra_args=["--mypy-skip"])
    assert not result


def test_report_file(rule_runner: RuleRunner) -> None:
    rule_runner.write_files({f"{PACKAGE}/f.py": GOOD_FILE, f"{PACKAGE}/BUILD": "python_library()"})
    tgt = rule_runner.get_target(Address(PACKAGE, relative_file_path="f.py"))
    result = run_mypy(rule_runner, [tgt], extra_args=["--mypy-args='--linecount-report=reports'"])
    assert len(result) == 1
    assert result[0].exit_code == 0
    assert "Success: no issues found" in result[0].stdout.strip()
    report_files = rule_runner.request(DigestContents, [result[0].report])
    assert len(report_files) == 1
    assert "4       4      1      1 f" in report_files[0].content.decode()


def test_thirdparty_dependency(rule_runner: RuleRunner) -> None:
    rule_runner.write_files(
        {
            "BUILD": dedent(
                """\
                python_requirement_library(
                    name="more-itertools", requirements=["more-itertools==8.4.0"],
                )
                """
            ),
            f"{PACKAGE}/f.py": dedent(
                """\
                from more_itertools import flatten

                assert flatten(42) == [4, 2]
                """
            ),
            f"{PACKAGE}/BUILD": "python_library()",
        }
    )
    tgt = rule_runner.get_target(Address(PACKAGE, relative_file_path="f.py"))
    result = run_mypy(rule_runner, [tgt])
    assert len(result) == 1
    assert result[0].exit_code == 1
    assert f"{PACKAGE}/f.py:3" in result[0].stdout


def test_thirdparty_plugin(rule_runner: RuleRunner) -> None:
    # NB: We install `django-stubs` both with `[mypy].extra_requirements` and a user requirement
    # (`python_requirement_library`). This awkwardness is because its used both as a plugin and
    # type stubs.
    rule_runner.write_files(
        {
            "BUILD": dedent(
                """\
                python_requirement_library(
                    name='django', requirements=['Django==2.2.5', 'django-stubs==1.8.0'],
                )
                """
            ),
            f"{PACKAGE}/settings.py": dedent(
                """\
                from django.urls import URLPattern

                DEBUG = True
                DEFAULT_FROM_EMAIL = "webmaster@example.com"
                SECRET_KEY = "not so secret"
                MY_SETTING = URLPattern(pattern="foo", callback=lambda: None)
                """
            ),
            f"{PACKAGE}/app.py": dedent(
                """\
                from django.utils import text

                assert "forty-two" == text.slugify("forty two")
                assert "42" == text.slugify(42)
                """
            ),
            f"{PACKAGE}/BUILD": "python_library()",
            "mypy.ini": dedent(
                """\
                [mypy]
                plugins =
                    mypy_django_plugin.main

                [mypy.plugins.django-stubs]
                django_settings_module = project.settings
                """
            ),
        }
    )
    tgt = rule_runner.get_target(Address(PACKAGE))
    result = run_mypy(
        rule_runner,
        [tgt],
        extra_args=[
            "--mypy-extra-requirements=django-stubs==1.8.0",
            "--mypy-version=mypy==0.812",
            "--mypy-lockfile=<none>",
        ],
    )
    assert len(result) == 1
    assert result[0].exit_code == 1
    assert f"{PACKAGE}/app.py:4" in result[0].stdout


def test_transitive_dependencies(rule_runner: RuleRunner) -> None:
    rule_runner.write_files(
        {
            f"{PACKAGE}/util/__init__.py": "",
            f"{PACKAGE}/util/lib.py": dedent(
                """\
                def capitalize(v: str) -> str:
                    return v.capitalize()
                """
            ),
            f"{PACKAGE}/util/BUILD": "python_library()",
            f"{PACKAGE}/math/__init__.py": "",
            f"{PACKAGE}/math/add.py": dedent(
                """\
                from project.util.lib import capitalize

                def add(x: int, y: int) -> str:
                    sum = x + y
                    return capitalize(sum)  # This is the wrong type.
                """
            ),
            f"{PACKAGE}/math/BUILD": "python_library()",
            f"{PACKAGE}/__init__.py": "",
            f"{PACKAGE}/app.py": dedent(
                """\
                from project.math.add import add

                print(add(2, 4))
                """
            ),
            f"{PACKAGE}/BUILD": "python_library()",
        }
    )
    tgt = rule_runner.get_target(Address(PACKAGE, relative_file_path="app.py"))
    result = run_mypy(rule_runner, [tgt])
    assert len(result) == 1
    assert result[0].exit_code == 1
    assert f"{PACKAGE}/math/add.py:5" in result[0].stdout


@skip_unless_python27_present
def test_works_with_python27(rule_runner: RuleRunner) -> None:
    """A regression test that we can properly handle Python 2-only third-party dependencies.

    There was a bug that this would cause the runner PEX to fail to execute because it did not have
    Python 3 distributions of the requirements.

    Also note that this Python 2 support should be automatic: Pants will tell MyPy to run with
    `--py2` by detecting its use in interpreter constraints.
    """
    rule_runner.write_files(
        {
            "BUILD": dedent(
                """\
                # Both requirements are a) typed and b) compatible with Py2 and Py3. However, `x690`
                # has a distinct wheel for Py2 vs. Py3, whereas libumi has a universal wheel. We expect
                # both to be usable, even though libumi is not compatible with Py3.

                python_requirement_library(
                    name="libumi",
                    requirements=["libumi==0.0.2"],
                )

                python_requirement_library(
                    name="x690",
                    requirements=["x690==0.2.0"],
                )
                """
            ),
            f"{PACKAGE}/f.py": dedent(
                """\
                from libumi import hello_world
                from x690 import types

                print "Blast from the past!"
                print hello_world() - 21  # MyPy should fail. You can't subtract an `int` from `bytes`.
                """
            ),
            f"{PACKAGE}/BUILD": "python_library(interpreter_constraints=['==2.7.*'])",
        }
    )
    tgt = rule_runner.get_target(Address(PACKAGE, relative_file_path="f.py"))
    result = run_mypy(rule_runner, [tgt])
    assert len(result) == 1
    assert result[0].exit_code == 1
    assert f"{PACKAGE}/f.py:5: error: Unsupported operand types" in result[0].stdout
    # Confirm original issues not showing up.
    assert "Failed to execute PEX file" not in result[0].stderr
    assert (
        "Cannot find implementation or library stub for module named 'x690'" not in result[0].stdout
    )
    assert (
        "Cannot find implementation or library stub for module named 'libumi'"
        not in result[0].stdout
    )


@skip_unless_python38_present
def test_works_with_python38(rule_runner: RuleRunner) -> None:
    """MyPy's typed-ast dependency does not understand Python 3.8, so we must instead run MyPy with
    Python 3.8 when relevant."""
    rule_runner.write_files(
        {
            f"{PACKAGE}/f.py": dedent(
                """\
                x = 0
                if y := x:
                    print("x is truthy and now assigned to y")
                """
            ),
            f"{PACKAGE}/BUILD": "python_library(interpreter_constraints=['>=3.8'])",
        }
    )
    tgt = rule_runner.get_target(Address(PACKAGE, relative_file_path="f.py"))
    assert_success(rule_runner, tgt)


@skip_unless_python39_present
def test_works_with_python39(rule_runner: RuleRunner) -> None:
    """MyPy's typed-ast dependency does not understand Python 3.9, so we must instead run MyPy with
    Python 3.9 when relevant."""
    rule_runner.write_files(
        {
            f"{PACKAGE}/f.py": dedent(
                """\
                @lambda _: int
                def replaced(x: bool) -> str:
                    return "42" if x is True else "1/137"
                """
            ),
            f"{PACKAGE}/BUILD": "python_library(interpreter_constraints=['>=3.9'])",
        }
    )
    tgt = rule_runner.get_target(Address(PACKAGE, relative_file_path="f.py"))
    assert_success(rule_runner, tgt)


@skip_unless_python27_and_python3_present
def test_uses_correct_python_version(rule_runner: RuleRunner) -> None:
    """We set `--python-version` automatically for the user, and also batch based on interpreter
    constraints.

    This batching must consider transitive dependencies, so we use a more complex setup where the
    dependencies are what have specific constraints that influence the batching.
    """
    rule_runner.write_files(
        {
            f"{PACKAGE}/py2/__init__.py": dedent(
                """\
                def add(x, y):
                    # type: (int, int) -> int
                    return x + y
                """
            ),
            f"{PACKAGE}/py2/BUILD": "python_library(interpreter_constraints=['==2.7.*'])",
            f"{PACKAGE}/py3/__init__.py": dedent(
                """\
                def add(x: int, y: int) -> int:
                    return x + y
                """
            ),
            f"{PACKAGE}/py3/BUILD": "python_library(interpreter_constraints=['>=3.6'])",
            f"{PACKAGE}/__init__.py": "",
            f"{PACKAGE}/uses_py2.py": "from project.py2 import add\nassert add(2, 2) == 4\n",
            f"{PACKAGE}/uses_py3.py": "from project.py3 import add\nassert add(2, 2) == 4\n",
            f"{PACKAGE}/BUILD": "python_library(interpreter_constraints=['==2.7.*', '>=3.6'])",
        }
    )
    py2_tgt = rule_runner.get_target(Address(PACKAGE, relative_file_path="uses_py2.py"))
    py3_tgt = rule_runner.get_target(Address(PACKAGE, relative_file_path="uses_py3.py"))

    result = run_mypy(rule_runner, [py2_tgt, py3_tgt])
    assert len(result) == 2
    py2_result, py3_result = sorted(result, key=lambda res: res.partition_description or "")

    assert py2_result.exit_code == 0
    assert py2_result.partition_description == "['CPython==2.7.*', 'CPython==2.7.*,>=3.6']"
    assert "Success: no issues found" in py2_result.stdout

    assert py3_result.exit_code == 0
    assert py3_result.partition_description == "['CPython==2.7.*,>=3.6', 'CPython>=3.6']"
    assert "Success: no issues found" in py3_result.stdout


def test_run_only_on_specified_files(rule_runner: RuleRunner) -> None:
    rule_runner.write_files(
        {
            f"{PACKAGE}/good.py": GOOD_FILE,
            f"{PACKAGE}/bad.py": BAD_FILE,
            f"{PACKAGE}/BUILD": dedent(
                """\
                python_library(name='good', sources=['good.py'], dependencies=[':bad'])
                python_library(name='bad', sources=['bad.py'])
                """
            ),
        }
    )
    tgt = rule_runner.get_target(Address(PACKAGE, target_name="good", relative_file_path="good.py"))
    assert_success(rule_runner, tgt)


def test_type_stubs(rule_runner: RuleRunner) -> None:
    """Test that first-party type stubs work for both first-party and third-party code."""
    rule_runner.write_files(
        {
            "BUILD": "python_requirement_library(name='colors', requirements=['ansicolors'])",
            "mypy_stubs/__init__.py": "",
            "mypy_stubs/colors.pyi": "def red(s: str) -> str: ...",
            "mypy_stubs/BUILD": "python_library()",
            f"{PACKAGE}/util/__init__.py": "",
            f"{PACKAGE}/util/untyped.py": "def add(x, y):\n    return x + y",
            f"{PACKAGE}/util/untyped.pyi": "def add(x: int, y: int) -> int: ...",
            f"{PACKAGE}/util/BUILD": "python_library()",
            f"{PACKAGE}/__init__.py": "",
            f"{PACKAGE}/app.py": dedent(
                """\
                from colors import red
                from project.util.untyped import add

                z = add(2, 2.0)
                print(red(z))
                """
            ),
            f"{PACKAGE}/BUILD": "python_library()",
        }
    )
    tgt = rule_runner.get_target(Address(PACKAGE, relative_file_path="app.py"))
    result = run_mypy(
        rule_runner, [tgt], extra_args=["--source-root-patterns=['mypy_stubs', 'src/py']"]
    )
    assert len(result) == 1
    assert result[0].exit_code == 1
    assert f"{PACKAGE}/app.py:4: error: Argument 2 to" in result[0].stdout
    assert f"{PACKAGE}/app.py:5: error: Argument 1 to" in result[0].stdout


def test_mypy_shadows_requirements(rule_runner: RuleRunner) -> None:
    """Test the behavior of a MyPy requirement shadowing a user's requirement.

    The way we load requirements is complex. We want to ensure that things still work properly in
    this edge case.
    """
    rule_runner.write_files(
        {
            "BUILD": "python_requirement_library(name='ta', requirements=['typed-ast==1.4.1'])",
            f"{PACKAGE}/f.py": "import typed_ast",
            f"{PACKAGE}/BUILD": "python_library()",
        }
    )
    tgt = rule_runner.get_target(Address(PACKAGE, relative_file_path="f.py"))
    assert_success(
        rule_runner, tgt, extra_args=["--mypy-version=mypy==0.782", "--mypy-lockfile=<none>"]
    )


def test_source_plugin(rule_runner: RuleRunner) -> None:
    # NB: We make this source plugin fairly complex by having it use transitive dependencies.
    # This is to ensure that we can correctly support plugins with dependencies.
    # The plugin changes the return type of functions ending in `__overridden_by_plugin` to have a
    # return type of `None`.
    plugin_file = dedent(
        """\
        from typing import Callable, Optional, Type

        from mypy.plugin import FunctionContext, Plugin
        from mypy.types import NoneType, Type as MyPyType

        from plugins.subdir.dep import is_overridable_function
        from project.subdir.util import noop

        noop()

        class ChangeReturnTypePlugin(Plugin):
            def get_function_hook(
                self, fullname: str
            ) -> Optional[Callable[[FunctionContext], MyPyType]]:
                return hook if is_overridable_function(fullname) else None

        def hook(ctx: FunctionContext) -> MyPyType:
            return NoneType()

        def plugin(_version: str) -> Type[Plugin]:
            return ChangeReturnTypePlugin
        """
    )
    rule_runner.write_files(
        {
            "BUILD": dedent(
                f"""\
                python_requirement_library(name='mypy', requirements=['{MyPy.default_version}'])
                python_requirement_library(
                    name="more-itertools", requirements=["more-itertools==8.4.0"]
                )
                """
            ),
            "pants-plugins/plugins/subdir/__init__.py": "",
            "pants-plugins/plugins/subdir/dep.py": dedent(
                """\
                from more_itertools import flatten

                def is_overridable_function(name: str) -> bool:
                    assert list(flatten([[1, 2], [3, 4]])) == [1, 2, 3, 4]
                    return name.endswith("__overridden_by_plugin")
                """
            ),
            "pants-plugins/plugins/subdir/BUILD": "python_library()",
            # The plugin can depend on code located anywhere in the project; its dependencies need
            # not be in the same directory.
            f"{PACKAGE}/subdir/__init__.py": "",
            f"{PACKAGE}/subdir/util.py": "def noop() -> None:\n    pass\n",
            f"{PACKAGE}/subdir/BUILD": "python_library()",
            "pants-plugins/plugins/__init__.py": "",
            "pants-plugins/plugins/change_return_type.py": plugin_file,
            "pants-plugins/plugins/BUILD": "python_library()",
            f"{PACKAGE}/__init__.py": "",
            f"{PACKAGE}/f.py": dedent(
                """\
                def add(x: int, y: int) -> int:
                    return x + y

                def add__overridden_by_plugin(x: int, y: int) -> int:
                    return x  + y

                result = add__overridden_by_plugin(1, 1)
                assert add(result, 2) == 4
                """
            ),
            f"{PACKAGE}/BUILD": "python_library()",
            "mypy.ini": dedent(
                """\
                [mypy]
                plugins =
                    plugins.change_return_type
                """
            ),
        }
    )

    def run_mypy_with_plugin(tgt: Target) -> CheckResult:
        result = run_mypy(
            rule_runner,
            [tgt],
            extra_args=[
                "--mypy-source-plugins=['pants-plugins/plugins']",
                "--mypy-lockfile=<none>",
                "--source-root-patterns=['pants-plugins', 'src/py']",
            ],
        )
        assert len(result) == 1
        return result[0]

    tgt = rule_runner.get_target(Address(PACKAGE, relative_file_path="f.py"))
    result = run_mypy_with_plugin(tgt)
    assert result.exit_code == 1
    assert f"{PACKAGE}/f.py:8" in result.stdout
    # Ensure we don't accidentally check the source plugin itself.
    assert "(checked 1 source file)" in result.stdout

    # Ensure that running MyPy on the plugin itself still works.
    plugin_tgt = rule_runner.get_target(
        Address("pants-plugins/plugins", relative_file_path="change_return_type.py")
    )
    result = run_mypy_with_plugin(plugin_tgt)
    assert result.exit_code == 0
    assert "Success: no issues found in 1 source file" in result.stdout


def test_protobuf_mypy(rule_runner: RuleRunner) -> None:
    rule_runner = RuleRunner(
        rules=[*rule_runner.rules, *protobuf_rules(), *protobuf_subsystem_rules()],
        target_types=[*rule_runner.target_types, ProtobufLibrary],
    )
    rule_runner.write_files(
        {
            "BUILD": (
                "python_requirement_library(name='protobuf', requirements=['protobuf==3.13.0'])"
            ),
            f"{PACKAGE}/__init__.py": "",
            f"{PACKAGE}/proto.proto": dedent(
                """\
                syntax = "proto3";
                package project;

                message Person {
                    string name = 1;
                    int32 id = 2;
                    string email = 3;
                }
                """
            ),
            f"{PACKAGE}/f.py": dedent(
                """\
                from project.proto_pb2 import Person

                x = Person(name=123, id="abc", email=None)
                """
            ),
            f"{PACKAGE}/BUILD": dedent(
                """\
                python_library(dependencies=[':proto'])
                protobuf_library(name='proto')
                """
            ),
        }
    )
    tgt = rule_runner.get_target(Address(PACKAGE, relative_file_path="f.py"))
    result = run_mypy(
        rule_runner,
        [tgt],
        extra_args=[
            "--backend-packages=pants.backend.codegen.protobuf.python",
            "--python-protobuf-mypy-plugin",
        ],
    )
    assert len(result) == 1
    assert 'Argument "name" to "Person" has incompatible type "int"' in result[0].stdout
    assert 'Argument "id" to "Person" has incompatible type "str"' in result[0].stdout
    assert result[0].exit_code == 1


def test_determine_python_files() -> None:
    assert determine_python_files([]) == ()
    assert determine_python_files(["f.py"]) == ("f.py",)
    assert determine_python_files(["f.pyi"]) == ("f.pyi",)
    assert determine_python_files(["f.py", "f.pyi"]) == ("f.pyi",)
    assert determine_python_files(["f.pyi", "f.py"]) == ("f.pyi",)
    assert determine_python_files(["f.json"]) == ()
