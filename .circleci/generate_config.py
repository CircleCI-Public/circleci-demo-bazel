#!/usr/bin/env python3
"""
Generates the CircleCI continuation config for the current branch.

On main:
  Single build-and-test job targeting //...

On PRs:
  1. build-affected  — bazel build <all affected rule targets>
                       Bazel handles dependency ordering internally and
                       populates the remote cache via with_tool_cache.
  2. test-<target>   — one job per affected test target, all requiring
                       build-affected. Tests run in parallel; artifacts
                       are already in the remote cache so no recompilation.

Reads:  hashes/affected.txt  (written by the setup job's bazel-diff step)
Writes: /tmp/continuation_config.yml
"""

import os
import re
import subprocess
import sys


DOCKER_IMAGE = "cimg/base:current"
OUTPUT_PATH = "/tmp/continuation_config.yml"
INSTALL_BAZEL_CMD = (
    "curl -fsSL \\\n"
    "              https://github.com/bazelbuild/bazelisk/releases/latest/download/bazelisk-linux-amd64 \\\n"
    "              -o /tmp/bazel\n"
    "            sudo mv /tmp/bazel /usr/local/bin/bazel\n"
    "            sudo chmod +x /usr/local/bin/bazel"
)
# Bazel writes JUnit XML to bazel-testlogs/<package>/<target>/test.xml;
# copy it somewhere store_test_results can read, keeping the package path.
COLLECT_TEST_RESULTS_CMD = (
    "mkdir -p /tmp/test-results\n"
    "            if [ -d bazel-testlogs ]; then\n"
    "              cd bazel-testlogs\n"
    "              find -L . -name test.xml -exec cp --parents {} /tmp/test-results \\;\n"
    "            fi"
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def job_name(target: str) -> str:
    """//lib/math:math_test  ->  test-lib-math-math-test"""
    return "test-" + re.sub(r"[^a-zA-Z0-9-]", "-", target.lstrip("/")).strip("-")


def bazel_query(expr: str) -> set:
    result = subprocess.run(["bazel", "query", expr], capture_output=True, text=True)
    if result.returncode != 0:
        print(f"  [warn] bazel query failed: {expr}", file=sys.stderr)
        print(f"  stderr: {result.stderr.strip()}", file=sys.stderr)
        return set()
    return {l.strip() for l in result.stdout.splitlines() if l.strip()}


def install_bazel_step() -> str:
    return (
        "      - run:\n"
        "          name: Install Bazel (via Bazelisk)\n"
        "          command: |\n"
        f"            {INSTALL_BAZEL_CMD}\n"
    )


def store_test_results_steps() -> str:
    return (
        "      - run:\n"
        "          name: Collect test results\n"
        "          when: always\n"
        "          command: |\n"
        f"            {COLLECT_TEST_RESULTS_CMD}\n"
        "      - store_test_results:\n"
        "          path: /tmp/test-results\n"
    )


def build_job(targets: list) -> str:
    targets_expr = " ".join(targets)
    return (
        "  build-affected:\n"
        f"    docker:\n"
        f"      - image: {DOCKER_IMAGE}\n"
        f"    steps:\n"
        f"      - checkout\n"
        + install_bazel_step() +
        f"      - with_tool_cache:\n"
        f"          tool: bazel\n"
        f"          steps:\n"
        f"            - run:\n"
        f"                name: Build affected targets\n"
        f"                command: bazel build {targets_expr}\n"
    )


def test_job(target: str) -> str:
    name = job_name(target)
    return (
        f"  {name}:\n"
        f"    docker:\n"
        f"      - image: {DOCKER_IMAGE}\n"
        f"    steps:\n"
        f"      - checkout\n"
        + install_bazel_step() +
        f"      - with_tool_cache:\n"
        f"          tool: bazel\n"
        f"          steps:\n"
        f"            - run:\n"
        f"                name: \"Test {target}\"\n"
        f"                command: bazel test {target}\n"
        + store_test_results_steps()
    )


def single_build_and_test(targets_expr: str, label: str = "") -> str:
    tag = f" ({label})" if label else ""
    return (
        "  build-and-test:\n"
        f"    docker:\n"
        f"      - image: {DOCKER_IMAGE}\n"
        f"    steps:\n"
        f"      - checkout\n"
        + install_bazel_step() +
        f"      - with_tool_cache:\n"
        f"          tool: bazel\n"
        f"          steps:\n"
        f"            - run:\n"
        f"                name: \"Build{tag}\"\n"
        f"                command: bazel build {targets_expr}\n"
        f"            - run:\n"
        f"                name: \"Test{tag}\"\n"
        f"                command: bazel test --build_tests_only {targets_expr}\n"
        + store_test_results_steps()
    )


def no_op_config() -> str:
    return (
        "version: 2.1\n"
        "jobs:\n"
        "  no-affected-targets:\n"
        "    docker:\n"
        f"      - image: {DOCKER_IMAGE}\n"
        "    steps:\n"
        "      - run:\n"
        "          name: No affected targets\n"
        "          command: echo \"No Bazel targets were affected by this change.\"\n"
        "workflows:\n"
        "  ci:\n"
        "    jobs:\n"
        "      - no-affected-targets\n"
    )


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    branch = os.environ.get("CIRCLE_BRANCH", "")
    print(f"Branch: {branch}")

    # ── main: single build-and-test job ──────────────────────────────────────
    if branch == "main":
        config = (
            "version: 2.1\n"
            "jobs:\n"
            + single_build_and_test("//...")
            + "\nworkflows:\n  ci:\n    jobs:\n      - build-and-test\n"
        )
        print("main branch → single job targeting //...")
        write(config)
        return

    # ── PR: load affected targets ─────────────────────────────────────────────
    try:
        with open("hashes/affected.txt") as f:
            targets = [l.strip() for l in f if l.strip()]
    except FileNotFoundError:
        targets = []

    if not targets:
        print("No affected targets → no-op")
        write(no_op_config())
        return

    if targets == ["//..."]:
        print("Fallback to //... (no base hash available)")
        config = (
            "version: 2.1\n"
            "jobs:\n"
            + single_build_and_test("//...", label="fallback — no base hash")
            + "\nworkflows:\n  ci:\n    jobs:\n      - build-and-test\n"
        )
        write(config)
        return

    # ── Filter source file targets (e.g. //lib/math:math.py) ─────────────────
    # bazel-diff includes source file targets; only rule targets are buildable.
    set_expr = " ".join(targets)
    rule_targets = sorted(bazel_query(f"kind(rule, set({set_expr}))"))
    if not rule_targets:
        print("No rule targets in affected set → no-op")
        write(no_op_config())
        return

    print(f"\nAffected rule targets ({len(rule_targets)}):")
    for t in rule_targets:
        print(f"  {t}")

    # ── Split into build targets and test targets ─────────────────────────────
    test_targets = sorted(bazel_query(f"kind(test, set({' '.join(rule_targets)}))"))

    print(f"\nTest targets ({len(test_targets)}):")
    for t in test_targets:
        print(f"  {t}")

    # ── Generate: one build job + one test job per test target ────────────────
    jobs_yaml = build_job(rule_targets)
    workflow_yaml = "      - build-affected\n"

    for t in test_targets:
        jobs_yaml += test_job(t)
        name = job_name(t)
        workflow_yaml += (
            f"      - {name}:\n"
            f"          requires:\n"
            f"            - build-affected\n"
        )

    config = (
        "version: 2.1\n"
        "jobs:\n"
        + jobs_yaml
        + "\nworkflows:\n  ci:\n    jobs:\n"
        + workflow_yaml
    )

    print(f"\nGenerated: 1 build job + {len(test_targets)} parallel test job(s)")
    write(config)


def write(config: str):
    with open(OUTPUT_PATH, "w") as f:
        f.write(config)
    print(f"\nWritten to {OUTPUT_PATH}")
    print("\n--- Generated config ---")
    print(config)


if __name__ == "__main__":
    main()
