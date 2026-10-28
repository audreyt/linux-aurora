# SPDX-License-Identifier: GPL-2.0
import os
import unittest.mock

from support import TempDirTestCase, load

approve_runs = load("approve-runs")
plan = load("plan")

HEAD = "b" * 40
FORK = 7
WORKFLOW = ".github/workflows/kernel-ci.yml"
VOUCHED = plan.read_vouched("github:Teammate\n-github:banned spam\n")


def event(author="teammate", association="CONTRIBUTOR", sender=None):
    return {"sender": {"login": sender or author}, "pull_request": {"number": 42, "user": {"login": author},
                             "author_association": association,
                             "head": {"sha": HEAD, "repo": {"id": FORK}}}}


def run(run_id, conclusion="action_required", status="completed", path=WORKFLOW, sha=HEAD, repo=FORK):
    return {"id": run_id, "path": path, "head_sha": sha, "status": status, "conclusion": conclusion,
            "head_repository": {"id": repo}}


class FakeApi:
    def __init__(self, runs=(), pulls=None):
        self.runs = list(runs)  # one list per poll; the last one repeats
        self.pulls = pulls or [{"state": "open", "head": {"sha": HEAD}}]
        self.polls = 0
        self.approved = []

    def get(self, path, **query):
        if path == "pulls/42":
            return self.pulls[min(self.polls, len(self.pulls) - 1)]
        if path == "actions/runs":
            assert query["event"] == "pull_request" and query["head_sha"] == HEAD
            runs = self.runs[min(self.polls, len(self.runs) - 1)] if self.runs else []
            self.polls += 1
            return {"workflow_runs": runs}
        raise AssertionError(path)

    def post(self, path):
        self.approved.append(path)


class ApproveTest(TempDirTestCase):
    def approve(self, api, pr_event=None, attempts=3):
        return approve_runs.approve(api, pr_event or event(), VOUCHED, plan, attempts=attempts,
                                    sleep=lambda seconds: None)

    def test_approves_the_newest_held_run_of_a_vouched_author(self):
        api = FakeApi([[run(5), run(9), run(7)]])
        self.assertIn("approved run 9", self.approve(api))
        self.assertEqual(api.approved, ["actions/runs/9/approve"])

    def test_waits_for_the_run_to_appear(self):
        api = FakeApi([[], [], [run(3)]])
        self.approve(api)
        self.assertEqual(api.approved, ["actions/runs/3/approve"])

    def test_never_approves_anyone_else(self):
        for pr_event in (event("stranger"), event("banned"), event("BANNED"),
                         event("member", association="MEMBER"), event(sender="stranger"),
                         event(sender="banned")):
            with self.subTest(event=pr_event):
                api = FakeApi([[run(3)]])
                self.assertIn("nothing to approve", self.approve(api, pr_event))
                self.assertEqual((api.approved, api.polls), ([], 0))

    def test_ignores_other_workflows_heads_and_repositories(self):
        api = FakeApi([[run(3, path=".github/workflows/kernel-ci-label.yml"), run(4, sha="c" * 40),
                        run(5, repo=99)]])
        with self.assertRaises(TimeoutError):
            self.approve(api)
        self.assertEqual(api.approved, [])

    def test_leaves_a_run_that_is_not_held(self):
        api = FakeApi([[run(3), run(4, conclusion=None, status="in_progress")]])
        self.assertIn("not held", self.approve(api))
        self.assertEqual(api.approved, [])

    def test_stops_when_the_head_moves_or_the_pull_request_closes(self):
        for pull in ({"state": "open", "head": {"sha": "c" * 40}}, {"state": "closed", "head": {"sha": HEAD}}):
            with self.subTest(pull=pull):
                api = FakeApi([[run(3)]], pulls=[pull])
                self.assertIn("nothing to approve", self.approve(api))
                self.assertEqual(api.approved, [])

    def test_main_fails_without_a_vouch_list(self):
        self.github_env()
        path = self.tmp / "event.json"
        path.write_text("{}")
        environment = {"GITHUB_REPOSITORY": "omacom/linux-aurora", "GITHUB_TOKEN": "unused"}
        with unittest.mock.patch.dict(os.environ, environment):
            self.assertEqual(approve_runs.main(["--event-path", str(path),
                                                "--vouched", str(self.tmp / "missing")]), 1)
