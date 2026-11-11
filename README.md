# bazel-demo

A multi-target Bazel project that demonstrates graph-aware incremental CI builds on CircleCI using [bazel-diff](https://github.com/Tinder/bazel-diff) and CircleCI [dynamic config](https://circleci.com/docs/dynamic-config/). A setup workflow computes the affected Bazel targets and generates a purpose-built continuation pipeline — one build job for all affected targets, then parallel test jobs — rather than running conditional skip logic inside a static config.

## The problem

In a large monorepo, running `bazel test //...` on every PR is wasteful. Most changes touch a small slice of the codebase, but CI re-builds and re-tests everything regardless. As the repo grows this becomes the primary bottleneck — build times balloon and developer feedback loops slow down.

The naive alternative — only run tests in directories that changed — misses transitive effects. If you change a shared library, every downstream consumer is affected too, even if their own source files didn't change.

## The solution: graph-aware incremental builds

Bazel models your codebase as a dependency graph. Every target (`py_library`, `py_binary`, `py_test`, etc.) declares its inputs. Bazel uses this graph to know exactly which targets can be affected by any given change.

`bazel-diff` exploits this graph to compute a precise affected-target set between two commits:

1. **Snapshot the graph** — for each target, hash its rule definition, all its attributes, and the content of every source file it directly or transitively consumes. The result is a JSON file mapping target → hash.
2. **Diff two snapshots** — compare the hash file from `main` against the hash file from the PR branch. Any target whose hash changed, or whose transitive dependency's hash changed, is "affected."
3. **Build and test only those targets** — pass the affected set directly to `bazel build` and `bazel test`.

This is more correct than file-path heuristics because it understands the build graph. A change to a `.bzl` macro, a `BUILD` file attribute, or a shared library all propagate through the graph correctly.

### What `bazel-diff generate-hashes` actually does

It runs `bazel query` (not `bazel build`) to traverse the dependency graph, then computes SHA-256 hashes per target. No compilation happens — it's analysis-phase only, so it's fast. External dependencies need to be resolvable so Bazel can read the graph, but nothing is compiled.

### The two-snapshot problem

Comparing two commits requires a hash snapshot at each. Checking out both commits in a single CI job is awkward, so this repo uses a caching strategy instead:

- The **setup workflow** on `main` generates a hash snapshot after every merge and saves it to CircleCI's cache under a key that includes the commit SHA (`v1-bazel-diff-main-<sha>`).
- The **setup workflow** on PR branches restores the latest `main` snapshot using a prefix cache key (`v1-bazel-diff-main-`), which always resolves to the most recent entry without needing to know the exact commit.

### Two layers of caching

| Cache | What it stores | Purpose |
|-------|---------------|---------|
| `save_cache` / `restore_cache` | bazel-diff hash JSON from `main` | Lets PR setup jobs know what the graph looked like before their changes |
| `with_tool_cache` (Bazel remote cache) | Compiled build artifacts | Avoids recompiling targets whose inputs haven't changed |

They solve different problems. The hash cache tells the setup workflow *which* targets to include in the continuation. The build artifact cache makes building those targets fast.

## Project structure

```
lib/math/       py_library + py_test   (no upstream deps)
lib/strings/    py_library + py_test   (no upstream deps)
app/hello/      py_binary              (depends on lib/math AND lib/strings)
app/goodbye/    py_binary              (depends on lib/strings ONLY)
```

The dependency graph is intentional. A change to `lib/math` affects `app/hello` but **not** `app/goodbye`, which is what the incremental build demo proves.

## How the CI pipeline works

This repo uses CircleCI [dynamic config](https://circleci.com/docs/dynamic-config/). Rather than a static pipeline where every job decides at runtime what to skip, the setup workflow computes the affected target set first and generates a purpose-built continuation pipeline for that specific commit.

```
Push
  │
  └─ setup workflow (config.yml)
       │
       ├─ [main]  bazel-diff generate-hashes → save to cache
       │          generate continuation: single build-and-test job for //...
       │
       └─ [PR]    restore main hashes from cache
                  bazel-diff generate-hashes for this branch
                  bazel-diff get-impacted-targets → affected list
                  filter to rule targets; split into build + test targets
                  generate continuation: build job + parallel test jobs
       │
       └─ continuation API call
            │
            └─ continuation pipeline (ci workflow)
                 build-affected                ← builds all affected rule targets
                   ├─ test-lib-math-math-test  ┐ parallel, each requires
                   └─ test-lib-strings-...     ┘ build-affected
```

### Setup workflow (`config.yml`)

The setup workflow has a single `setup` job that runs on every push. It installs Bazel and bazel-diff, computes what needs to run, generates the continuation config via `.circleci/generate_config.py`, and calls the continuation API.

**On `main`:**
1. Runs `bazel-diff generate-hashes` → saves a snapshot of the full Bazel dependency graph to cache (`v1-bazel-diff-main-<sha>`).
2. Generates a single `build-and-test` continuation job targeting `//...`.

**On a PR branch:**
1. Restores the latest `main` snapshot from cache.
2. Runs `bazel-diff generate-hashes` for the current branch.
3. Runs `bazel-diff get-impacted-targets` → affected target list.
4. Filters to rule targets only (`kind(rule, set(...))`) — source file targets like `//lib/math:math.py` are excluded.
5. Splits into build targets and test targets (`kind(test, set(...))`).
6. Generates a continuation config with one `build-affected` job and one `test-<target>` job per test target.

### Continuation pipeline (generated per-commit)

The continuation is generated by `.circleci/generate_config.py` and varies by branch:

**`main`** — single job:
```yaml
jobs:
  build-and-test:
    steps:
      - bazel build //...
      - bazel test --build_tests_only //...
```

**PR branches** — one build job, then parallel test jobs. For a `lib/math` change:
```yaml
jobs:
  build-affected:
    steps:
      - bazel build //lib/math:math //lib/math:math_test //app/hello:hello
        # Bazel handles internal dependency ordering; populates remote cache

  test-lib-math-math-test:
    steps:
      - bazel test //lib/math:math_test
        # Artifacts already in remote cache — no recompilation

workflows:
  ci:
    jobs:
      - build-affected
      - test-lib-math-math-test:
          requires: [build-affected]   # waits for build, then runs
```

`//app/goodbye` is absent entirely — it has no dependency on `lib/math`. `//app/hello` is built (it's affected) but has no tests, so it appears only in `build-affected`. Test jobs run in parallel once the build completes.

The pipeline itself is the answer to "what ran" — no conditionals, no skip logic in the jobs themselves.

## Demoing the incremental build

### Step 0 — enable dynamic config in project settings

Dynamic config must be turned on before `setup: true` in `config.yml` takes effect. In the CircleCI web UI: **Project Settings → Advanced → Enable dynamic config using setup workflows**.

Without this, CircleCI treats `config.yml` as a normal pipeline and ignores `setup: true`.

### Step 1 — ensure a green `main` run exists

The PR setup workflow needs a cached hash snapshot from `main` to diff against. Check that the latest `main` run succeeded:

```bash
circleci run list --project gh/<org>/webster-bazel-demo --branch main --limit 1
```

If it failed, push a fix to `main` and wait for the setup + continuation workflows to go green.

### Step 2 — create a branch that only touches `lib/math`

```bash
git checkout -b demo/math-only
# Edit lib/math/math.py — add a function, change a constant, anything
git add lib/math/math.py
git commit -m "demo: change lib/math only"
git push -u origin demo/math-only
```

### Step 3 — watch the run

```bash
# Wait ~10s for CircleCI to pick up the push
circleci run list --project gh/<org>/webster-bazel-demo --branch demo/math-only --limit 1

# Watch setup + continuation live
circleci run watch <run-id>
```

You'll see two workflow phases: `setup` completes first, then `ci` (the continuation) starts.

### Step 4 — inspect what the setup workflow computed

List the setup job's steps to find the relevant ones, then read their output:

```bash
# Find the setup job ID
circleci run get <run-id> --json \
  | python3 -c "import json,sys; r=json.load(sys.stdin); [print(j['id'], j['name']) for w in r['workflows'] for j in w['jobs']]"

# List all steps with numbers
circleci job output list <setup-job-id>

# Read "Compute affected targets" output
circleci job output get <setup-job-id> --step-num <N> --strip-ansi

# Read "Generate continuation config" output to see the full generated YAML
circleci job output get <setup-job-id> --step-num <N> --strip-ansi
```

**Expected affected targets (lib/math change):**
```
//app/hello:hello
//lib/math:math
//lib/math:math_test
```

`//app/goodbye` and `//lib/strings` are absent — no dependency on `lib/math`. Source file targets like `//lib/math:math.py` are filtered out by the `kind(rule, set(...))` query in `generate_config.py` before the continuation is generated.

**Expected generated workflow (from the Generate continuation config step):**
```yaml
workflows:
  ci:
    jobs:
      - build-affected
      - test-lib-math-math-test:
          requires:
            - build-affected
```

### Step 5 — verify the inverse: change `lib/strings` instead

```bash
git checkout main && git checkout -b demo/strings-only
# Edit lib/strings/strings.py
git add lib/strings/strings.py
git commit -m "demo: change lib/strings only"
git push -u origin demo/strings-only
```

Now **both** `app/hello` and `app/goodbye` should appear in `build-affected` (both depend on `lib/strings`), but `lib/math` and `lib/math:math_test` should be absent. `lib/strings:strings_test` will appear as a `test-lib-strings-strings-test` job requiring `build-affected`.

## Tool versions

| Tool | Version | Notes |
|------|---------|-------|
| Bazel | latest via Bazelisk | Bazelisk reads `.bazelversion` if present; otherwise uses latest stable |
| bazel-diff | v49.1.0 | Rust rewrite; ships as a native binary (no JVM required) |
| rules_python | 2.3.4 | Required for `py_*` rules under Bzlmod (Bazel 7+) |

## Local setup

```bash
# Install Bazelisk (acts as the `bazel` command)
brew install bazelisk          # macOS
# or
curl -fsSL https://github.com/bazelbuild/bazelisk/releases/latest/download/bazelisk-$(uname -s | tr '[:upper:]' '[:lower:]')-$(uname -m | sed 's/x86_64/amd64/') -o /usr/local/bin/bazel && chmod +x /usr/local/bin/bazel

# Build and test everything
bazel build //...
bazel test //...
```
