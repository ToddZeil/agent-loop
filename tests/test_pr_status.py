import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from loop import pr_status


def git(cwd, *args):
    subprocess.run(['git', '-C', str(cwd), *args], check=True, capture_output=True)


class PrStatusTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.repo = self.tmp / 'repo'
        self.repo.mkdir()
        git(self.repo, 'init', '-q', '-b', 'main')
        git(self.repo, '-c', 'user.email=t@t', '-c', 'user.name=t', 'commit', '-q', '--allow-empty', '-m', 'x')
        git(self.repo, 'remote', 'add', 'origin', 'https://github.com/acme/app.git')
        git(self.repo, 'switch', '-q', '-c', 'feature')
        self.path = self.tmp / 'pr.json'
        self.calls = []

    def agent(self, activity=None):
        return [{'repo': self.repo, 'worktree': self.repo, 'path': self.path, 'activity': activity}]

    def lister(self, prs):
        def lister(repository):
            self.calls.append(repository)
            return prs
        return lister

    def push(self):
        pass

    def test_no_pr_is_asked_again_only_after_a_turn(self):
        self.assertEqual(pr_status.refresh(self.agent(), self.lister([]), now=100), 1)
        self.assertEqual(pr_status.refresh(self.agent(), self.lister([]), now=5000), 0)
        self.assertEqual(pr_status.refresh(self.agent({'state': 'idle', 'at': 4000}), self.lister([]), now=5000), 1)

    def test_one_call_finds_the_branch_pr(self):
        self.push()
        prs = [{'number': 5, 'state': 'OPEN', 'isDraft': False, 'url': 'https://x/5', 'headRefName': 'feature'},
               {'number': 6, 'state': 'OPEN', 'isDraft': False, 'url': 'https://x/6', 'headRefName': 'other'}]
        agents = self.agent() * 3
        self.assertEqual(pr_status.refresh(agents, self.lister(prs), now=100), 1)
        self.assertEqual(self.calls, ['acme/app'])
        self.assertEqual(json.loads(self.path.read_text())['number'], 5)

    def test_open_waits_for_a_turn_or_ten_minutes(self):
        self.push()
        prs = [{'number': 5, 'state': 'OPEN', 'isDraft': False, 'url': 'https://x/5', 'headRefName': 'feature'}]
        pr_status.refresh(self.agent(), self.lister(prs), now=100)
        self.assertEqual(pr_status.refresh(self.agent({'state': 'idle', 'at': 50}), self.lister(prs), now=200), 0)
        self.assertEqual(pr_status.refresh(self.agent({'state': 'working', 'at': 150}), self.lister(prs), now=200), 0)
        self.assertEqual(pr_status.refresh(self.agent({'state': 'idle', 'at': 150}), self.lister(prs), now=200), 1)
        self.assertEqual(pr_status.refresh(self.agent(), self.lister(prs), now=200 + 600), 1)

    def test_merged_is_final(self):
        self.push()
        prs = [{'number': 5, 'state': 'MERGED', 'isDraft': False, 'url': 'https://x/5', 'headRefName': 'feature'}]
        pr_status.refresh(self.agent(), self.lister(prs), now=100)
        self.assertEqual(pr_status.refresh(self.agent({'state': 'idle', 'at': 900}), self.lister(prs), now=5000), 0)

    def test_draft_is_reported_as_draft(self):
        self.push()
        prs = [{'number': 5, 'state': 'OPEN', 'isDraft': True, 'url': 'https://x/5', 'headRefName': 'feature'}]
        pr_status.refresh(self.agent(), self.lister(prs), now=100)
        self.assertEqual(json.loads(self.path.read_text())['state'], 'DRAFT')


if __name__ == '__main__':
    unittest.main()
