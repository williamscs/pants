# Copyright 2020 Pants project contributors (see CONTRIBUTORS.md).
# Licensed under the Apache License, Version 2.0 (see LICENSE).

python_library(dependencies=[":lockfile"])
resources(name="lockfile", sources=["lockfile.txt"])

python_tests(name="subsystem_test", sources=["subsystem_test.py"], timeout=180)
python_tests(
    name="rules_integration_test",
    sources=["rules_integration_test.py"],
    timeout=480,
    # We want to make sure the default lockfile works for both macOS and Linux.
    tags=["platform_specific_behavior"],
)

