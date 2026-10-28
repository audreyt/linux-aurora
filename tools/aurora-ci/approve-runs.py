#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0
"""Release the held kernel CI run of a pull request by a vouched-for author.

GitHub holds every pull_request run from outside collaborators until a
maintainer approves it. kernel-ci-approve.yml runs this on
pull_request_target, from the target branch's code, never the pull
request's: when the author and the pusher are vouched for in
.github/VOUCHED.td it approves the held kernel-ci.yml run of the pull request's current head, and plan.py then
decides what that run does. Organization members' and collaborators' runs
are never held; anyone else's stay held for a maintainer.
"""

import argparse
import importlib.util
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

WORKFLOW = ".github/workflows/kernel-ci.yml"
HELD = "action_required"


def load_plan():
    path = Path(__file__).resolve().parent / "plan.py"
    spec = importlib.util.spec_from_file_location("plan", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Api:
    def __init__(self, url, repository, token):
        self.url, self.repository, self.token = url.rstrip("/"), repository, token

    def request(self, method, path, **query):
        address = f"{self.url}/repos/{self.repository}/{path}"
        if query:
            address += "?" + urllib.parse.urlencode(query)
        request = urllib.request.Request(address, method=method, headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {self.token}",
            "X-GitHub-Api-Version": "2022-11-28",
        })
        with urllib.request.urlopen(request, timeout=60) as response:
            body = response.read()
        return json.loads(body) if body else {}

    def get(self, path, **query):
        return self.request("GET", path, **query)

    def post(self, path):
        return self.request("POST", path)


def held(run):
    return HELD in (run.get("status"), run.get("conclusion"))


def approve(api, event, vouched, plan, attempts=36, sleep=time.sleep):
    """Approve the newest held kernel-ci.yml run of the pull request's head.
    Returns a sentence saying what happened."""
    pr = event["pull_request"]
    status, author = plan.trust(event, vouched)
    if status != "vouched":
        return f"{author} is {status}: nothing to approve"
    number, head_sha = pr["number"], pr["head"]["sha"]
    head_repo = (pr["head"].get("repo") or {}).get("id")

    for attempt in range(attempts):
        if attempt:
            sleep(5)
        current = api.get(f"pulls/{number}")
        if current.get("state") != "open" or current["head"]["sha"] != head_sha:
            return f"#{number} closed or moved past {head_sha[:12]}: nothing to approve"
        data = api.get("actions/runs", event="pull_request", head_sha=head_sha, per_page=100)
        runs = sorted((run for run in data.get("workflow_runs", [])
                       if run.get("path") == WORKFLOW and run.get("head_sha") == head_sha
                       and (run.get("head_repository") or {}).get("id") == head_repo),
                      key=lambda run: run["id"])
        if not runs:
            continue  # GitHub creates the pull_request run alongside this one
        newest = runs[-1]
        if not held(newest):
            return f"run {newest['id']} for {head_sha[:12]} is not held"
        api.post(f"actions/runs/{newest['id']}/approve")
        return f"approved run {newest['id']} for {author} ({status}) at {head_sha[:12]}"
    raise TimeoutError(f"no {WORKFLOW} run for {head_sha[:12]} appeared")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--event-path", type=Path, default=os.environ.get("GITHUB_EVENT_PATH"))
    parser.add_argument("--vouched", type=Path, required=True, help="the target branch's VOUCHED.td")
    args = parser.parse_args(argv)

    plan = load_plan()
    api = Api(os.environ.get("GITHUB_API_URL", "https://api.github.com"),
              os.environ["GITHUB_REPOSITORY"], os.environ["GITHUB_TOKEN"])
    try:
        result = approve(api, json.loads(args.event_path.read_text()),
                         plan.read_vouched(args.vouched.read_text()), plan)
    except (OSError, KeyError, TypeError, ValueError) as error:
        print(f"::error title=Approve kernel CI::{error!r}", file=sys.stderr)
        return 1
    print(result)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as out:
            out.write(f"{result}.\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
