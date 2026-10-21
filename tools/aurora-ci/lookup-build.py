#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0
"""Name a kernel build by its inputs and find earlier results for it.

A build is identified by what goes into it: the kernel tree without the CI
itself (.github and tools/aurora-ci never reach the package), and the parts
of omarchy-pkgs that build it (the linux-aurora recipe, the builder image
definition, bin/build and the helpers it sources). The same key means the
same package, so CI never builds or tests one twice; a change to the CI
alone, or to omarchy-pkgs outside those parts, reuses the last build. What
the key cannot see (package updates in the Arch Linux ARM repositories the
builder installs from) is what the nightly rebuild is for.

  kernel-package-<key>     the packages and their metadata (reused instead of
                           building again)
  kernel-verified-<key>    written when every full-run job passed; a later
                           run with the same key skips KUnit and the boot test
  kernel-metadata-<id>     package metadata for a kernel (its tree without
                           the CI), the baseline a later change is compared with

This script computes the key, pins the omarchy-pkgs commit it was computed
from, and looks those artifacts up through the Actions API. It accepts only
artifacts this repository's kernel CI uploaded (see TRUSTED_WORKFLOWS);
a reused package or verification record must also come from a non-pull-request
run (REUSE_EVENTS), while a comparison baseline may come from a pull request.
"""

import argparse
import hashlib
import json
import os
import subprocess
import sys
import urllib.parse
import urllib.request
from pathlib import Path

# What omarchy-pkgs' bin/build uses to build linux-aurora: the recipe, the
# builder image and its build script, bin/build itself and the helpers that
# bin/build and build/build.sh source.
RECIPE_PARTS = ("pkgbuilds/linux-aurora", "build", "bin/build", "helpers/docker-helpers.sh",
                "helpers/message-helpers.sh", "helpers/package-metadata.sh", "helpers/paths.sh")
# Paths in the kernel tree that never reach the package.
CI_PATHS = {"": {".github"}, "tools": {"aurora-ci"}}


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True, text=True,
                          stdout=subprocess.PIPE).stdout.strip()


def kernel_id(repo, rev):
    """Hash of the tree at rev, leaving out CI_PATHS."""
    digest = hashlib.sha256()
    for directory, skip in sorted(CI_PATHS.items()):
        spec = f"{rev}:{directory}" if directory else f"{rev}^{{tree}}"
        listing = subprocess.run(["git", "-C", str(repo), "ls-tree", "--full-tree", spec], text=True,
                                 stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        if listing.returncode and directory:
            continue  # no such directory at this commit
        listing.check_returncode()
        for line in listing.stdout.splitlines():
            name = line.split("\t", 1)[1]
            if name in skip or (not directory and name in CI_PATHS):
                continue
            digest.update(f"{directory}/{line}\n".encode())
    return digest.hexdigest()[:40]


def ensure_commit(repo, sha, remote="origin"):
    """Fetch a commit's trees (no file contents) if the checkout lacks it."""
    if subprocess.run(["git", "-C", str(repo), "cat-file", "-e", f"{sha}^{{commit}}"],
                      stderr=subprocess.DEVNULL).returncode:
        git(repo, "fetch", "--quiet", "--no-tags", "--depth=1", "--filter=blob:none", remote, sha)


def build_key(kernel_tree, recipe_trees):
    text = f"kernel {kernel_tree}\n" + "".join(f"{part} {recipe_trees[part]}\n" for part in RECIPE_PARTS)
    return hashlib.sha256(text.encode()).hexdigest()[:32]


# Runs whose artifacts may be reused. Every artifact name is chosen by the
# workflow that uploads it, and a pull request from a fork runs its own copy
# of the workflow files, so a name alone proves nothing: reuse only artifacts
# from runs of this repository's kernel CI on this repository's own branches
# and events. Pushing a branch here needs write access; a fork does not.
TRUSTED_WORKFLOWS = {".github/workflows/kernel-ci.yml", ".github/workflows/kernel-ci-label.yml"}
# A package or a verification record is substituted into a later run instead
# of rebuilding/retesting, so it may only be reused from a run that is not a
# pull request: a writer's CI-only pull request has the same build key as the
# branch tip and could otherwise plant a package before anyone reviews it.
REUSE_EVENTS = {"push", "merge_group", "schedule", "workflow_dispatch"}
# A comparison baseline is only read, never substituted, so a pull request's
# metadata is fine as a baseline.
BASELINE_EVENTS = REUSE_EVENTS | {"pull_request"}


class Api:
    def __init__(self, url, repository, token, repository_id):
        self.url, self.repository, self.token = url.rstrip("/"), repository, token
        self.repository_id = int(repository_id)
        self._runs = {}

    def get(self, path, **query):
        address = f"{self.url}/repos/{self.repository}/{path}"
        if query:
            address += "?" + urllib.parse.urlencode(query)
        request = urllib.request.Request(address, headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {self.token}",
            "X-GitHub-Api-Version": "2022-11-28",
        })
        with urllib.request.urlopen(request, timeout=60) as response:
            return json.load(response)

    def trusted_run(self, run_id, events):
        """Whether a run is this repository's kernel CI, from one of the given
        events, on this repository (not a fork)."""
        if run_id not in self._runs:
            self._runs[run_id] = self.get(f"actions/runs/{run_id}")
        run = self._runs[run_id]
        return (run.get("event") in events
                and run.get("path") in TRUSTED_WORKFLOWS
                and (run.get("repository") or {}).get("id") == self.repository_id
                and (run.get("head_repository") or {}).get("id") == self.repository_id)

    def newest_artifact(self, name, events):
        """Run ID of the newest unexpired artifact with this exact name that a
        trusted run uploaded, or ''."""
        data = self.get("actions/artifacts", name=name, per_page=100)
        live = [a for a in data.get("artifacts", [])
                if not a.get("expired") and a.get("name") == name
                and a.get("workflow_run", {}).get("repository_id") == self.repository_id
                and a.get("workflow_run", {}).get("head_repository_id") == self.repository_id]
        live.sort(key=lambda a: a.get("created_at", ""), reverse=True)
        for artifact in live:
            if self.trusted_run(artifact["workflow_run"]["id"], events):
                return str(artifact["workflow_run"]["id"])
        return ""


def lookup(api, kernel, recipe_repo, base_sha, force):
    kernel_tree = kernel_id(kernel, "HEAD")
    recipe_commit = git(recipe_repo, "rev-parse", "HEAD")
    recipe_trees = {part: git(recipe_repo, "rev-parse", f"HEAD:{part}") for part in RECIPE_PARTS}
    key = build_key(kernel_tree, recipe_trees)
    out = {"key": key, "kernel_tree": kernel_tree, "recipe_commit": recipe_commit,
           "package_run": "", "verified_run": "", "base_tree": "", "baseline_run": ""}
    if not force:
        out["package_run"] = api.newest_artifact(f"kernel-package-{key}", REUSE_EVENTS)
        out["verified_run"] = api.newest_artifact(f"kernel-verified-{key}", REUSE_EVENTS)
    if base_sha:
        ensure_commit(kernel, base_sha)
        out["base_tree"] = kernel_id(kernel, base_sha)
        out["baseline_run"] = api.newest_artifact(f"kernel-metadata-{out['base_tree']}", BASELINE_EVENTS)
    return out


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--kernel", type=Path, required=True, help="checkout of the commit to build")
    parser.add_argument("--recipe-repo", type=Path, required=True, help="omarchy-pkgs checkout")
    parser.add_argument("--base-sha", default="", help="commit whose build is the comparison baseline")
    parser.add_argument("--force", default="false", help="'true' ignores earlier builds and results")
    args = parser.parse_args(argv)

    api = Api(os.environ.get("GITHUB_API_URL", "https://api.github.com"),
              os.environ["GITHUB_REPOSITORY"], os.environ["GITHUB_TOKEN"],
              os.environ["GITHUB_REPOSITORY_ID"])
    try:
        result = lookup(api, args.kernel, args.recipe_repo, args.base_sha, args.force == "true")
    except (subprocess.CalledProcessError, OSError, KeyError, ValueError) as error:
        print(f"::error title=Build lookup::{error}", file=sys.stderr)
        return 1

    print(json.dumps(result, indent=1))
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a") as out:
            for name, value in result.items():
                out.write(f"{name}={value}\n")
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        reuse = []
        if result["package_run"]:
            reuse.append(f"packages from run {result['package_run']}")
        if result["verified_run"]:
            reuse.append(f"verified by run {result['verified_run']}")
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as out:
            out.write(f"Build key `{result['key']}` (kernel `{result['kernel_tree'][:12]}`, "
                      f"omarchy-pkgs `{result['recipe_commit'][:12]}`)"
                      + (f"; reusing {', '.join(reuse)}" if reuse else "") + ".\n\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
