# SPDX-License-Identifier: GPL-2.0
from support import TempDirTestCase, commit_file, git, load

lookup = load("lookup-build")


class FakeApi:
    def __init__(self, artifacts=None):
        self.artifacts, self.asked = artifacts or {}, []

    def newest_artifact(self, name, events):
        self.asked.append((name, frozenset(events)))
        return self.artifacts.get(name, "")


class LookupTest(TempDirTestCase):
    def setUp(self):
        super().setUp()
        self.kernel = self.init_repo("kernel")
        self.base = commit_file(self.kernel, "Makefile", "VERSION = 7\n", "kernel")
        commit_file(self.kernel, "tools/lib/x.c", "int x;\n", "a tool")
        self.recipe = self.init_repo("recipe")
        for part in lookup.RECIPE_PARTS:
            path = part if part.startswith(("bin/", "helpers/")) else f"{part}/file"
            commit_file(self.recipe, path, part, f"add {part}")
        commit_file(self.recipe, "bin/omarchy-release", "x", "release tooling")
        commit_file(self.recipe, "pkgbuilds/other/PKGBUILD", "x", "another package")

    def key(self):
        tree = lookup.kernel_id(self.kernel, "HEAD")
        return lookup.build_key(tree, {p: git(self.recipe, "rev-parse", f"HEAD:{p}") for p in lookup.RECIPE_PARTS})

    def test_finds_earlier_build_verification_and_baseline(self):
        key = self.key()
        base_id = lookup.kernel_id(self.kernel, self.base)
        api = FakeApi({f"kernel-package-{key}": "11", f"kernel-verified-{key}": "12",
                       f"kernel-metadata-{base_id}": "13"})
        result = lookup.lookup(api, self.kernel, self.recipe, self.base, force=False)
        self.assertEqual((result["key"], result["package_run"], result["verified_run"]), (key, "11", "12"))
        self.assertEqual((result["base_tree"], result["baseline_run"]), (base_id, "13"))
        self.assertEqual(result["recipe_commit"], git(self.recipe, "rev-parse", "HEAD"))

    def test_force_ignores_earlier_results(self):
        api = FakeApi({f"kernel-package-{self.key()}": "11"})
        result = lookup.lookup(api, self.kernel, self.recipe, "", force=True)
        self.assertEqual((result["package_run"], result["verified_run"], result["baseline_run"]), ("", "", ""))
        self.assertEqual(api.asked, [])

    def test_other_packages_and_tooling_do_not_change_the_key(self):
        before = self.key()
        commit_file(self.recipe, "pkgbuilds/other/PKGBUILD", "y", "bump another package")
        commit_file(self.recipe, "bin/omarchy-release", "y", "release tooling")
        commit_file(self.recipe, "helpers/upstream-watch.py", "y", "an unrelated helper")
        self.assertEqual(self.key(), before)
        commit_file(self.recipe, "helpers/docker-helpers.sh", "changed", "change the builder")
        self.assertNotEqual(self.key(), before)
        before = self.key()
        commit_file(self.recipe, "pkgbuilds/linux-aurora/file", "changed", "change the kernel recipe")
        self.assertNotEqual(self.key(), before)

    def test_kernel_tree_changes_the_key(self):
        for path in ("README", "tools/lib/y.c", "tools/aurora-sep/z.py"):
            with self.subTest(path=path):
                before = self.key()
                commit_file(self.kernel, path, "x\n", "change")
                self.assertNotEqual(self.key(), before)

    def test_ci_changes_do_not_change_the_key(self):
        before = self.key()
        commit_file(self.kernel, ".github/workflows/kernel-ci.yml", "on: push\n", "workflow")
        commit_file(self.kernel, "tools/aurora-ci/plan.py", "print()\n", "ci tool")
        self.assertEqual(self.key(), before)


REPO = 1000


def artifact(run, created, head_repo=REPO, expired=False, name="kernel-package-x"):
    return {"name": name, "expired": expired, "created_at": created,
            "workflow_run": {"id": run, "repository_id": REPO, "head_repository_id": head_repo}}


def run(event="push", path=".github/workflows/kernel-ci.yml", head_repo=REPO):
    return {"event": event, "path": path, "repository": {"id": REPO}, "head_repository": {"id": head_repo}}


class ApiTest(TempDirTestCase):
    def api(self, artifacts, runs):
        api = lookup.Api("https://api.example", "o/r", "token", str(REPO))
        api.calls = []

        def get(path, **query):
            api.calls.append(path)
            if path == "actions/artifacts":
                return {"artifacts": [a for a in artifacts if a["name"] == query["name"]]}
            return runs[int(path.rsplit("/", 1)[1])]
        api.get = get
        return api

    def test_newest_unexpired_artifact_from_a_trusted_run(self):
        api = self.api([artifact(1, "2026-10-20", expired=True), artifact(2, "2026-10-18"),
                        artifact(3, "2026-10-19"), artifact(4, "2026-10-21", name="other")],
                       {2: run(), 3: run("merge_group")})
        self.assertEqual(api.newest_artifact("kernel-package-x", lookup.REUSE_EVENTS), "3")
        self.assertEqual(self.api([], {}).newest_artifact("kernel-package-x", lookup.REUSE_EVENTS), "")

    def test_a_fork_run_is_never_reused(self):
        api = self.api([artifact(2, "2026-10-18"), artifact(9, "2026-10-20", head_repo=2000)],
                       {2: run(), 9: run("pull_request", head_repo=2000)})
        self.assertEqual(api.newest_artifact("kernel-package-x", lookup.BASELINE_EVENTS), "2")
        self.assertNotIn("actions/runs/9", api.calls)

    def test_the_run_itself_must_be_trusted(self):
        cases = {
            "fork head": run("pull_request", head_repo=2000),
            "another workflow": run(path=".github/workflows/something-else.yml"),
            "an untrusted event": run("pull_request_target"),
        }
        for name, untrusted in cases.items():
            with self.subTest(name):
                api = self.api([artifact(5, "2026-10-20")], {5: untrusted})
                self.assertEqual(api.newest_artifact("kernel-package-x", lookup.BASELINE_EVENTS), "")

    def test_package_and_verified_reuse_excludes_pull_requests(self):
        # A writer's CI-only PR run uploads a package under the branch tip's key.
        api = self.api([artifact(7, "2026-10-20")], {7: run("pull_request")})
        self.assertEqual(api.newest_artifact("kernel-package-x", lookup.REUSE_EVENTS), "",
                         "a pull-request run must not be reused as a package")
        # The same PR run, as a comparison baseline, is acceptable.
        api = self.api([artifact(7, "2026-10-20", name="kernel-metadata-x")], {7: run("pull_request")})
        self.assertEqual(api.newest_artifact("kernel-metadata-x", lookup.BASELINE_EVENTS), "7")

    def test_reuse_events_cover_the_non_pr_triggers(self):
        for event in sorted(lookup.REUSE_EVENTS):
            with self.subTest(event):
                api = self.api([artifact(5, "2026-10-20")], {5: run(event)})
                self.assertEqual(api.newest_artifact("kernel-package-x", lookup.REUSE_EVENTS), "5")
        self.assertNotIn("pull_request", lookup.REUSE_EVENTS)
        self.assertIn("pull_request", lookup.BASELINE_EVENTS)
