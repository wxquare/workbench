import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

CHECKER = Path(__file__).resolve().parents[1] / 'check_docs.py'


class DocumentationGateTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.git('init', '-q')
        self.git('config', 'user.email', 'test@example.invalid')
        self.git('config', 'user.name', 'Test')
        (self.root / 'docs').mkdir()
        (self.root / 'README').write_text('base\n')
        self.commit('base')
        self.base = self.git('rev-parse', 'HEAD')
        self.write_doc('docs/01-guide.md', '# Hello\n\n## 重复标题\n\n## 重复标题\n', self.base[:7])
        self.commit('document')
        self.doc_base = self.git('rev-parse', 'HEAD')

    def git(self, *args):
        return subprocess.run(['git', *args], cwd=self.root, text=True, capture_output=True, check=True).stdout.strip()

    def commit(self, message):
        self.git('add', '-A')
        self.git('commit', '-qm', message)

    def write_doc(self, path, content, commit):
        dest = self.root / path
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(f'---\ntitle: Guide\nstatus: stable\nverified_commit: {commit}\n---\n\n{content}', encoding='utf-8')

    def check(self, *args, skip=False):
        env = dict(os.environ)
        env.pop('DOCS_SKIP_BUMP', None)
        if skip:
            env['DOCS_SKIP_BUMP'] = '1'
        return subprocess.run([sys.executable, str(CHECKER), '--root', str(self.root), *args], env=env, text=True, capture_output=True)

    def test_valid_document_and_duplicate_encoded_anchor(self):
        self.write_doc('docs/02-other.md', '[link](01-guide.md#%E9%87%8D%E5%A4%8D%E6%A0%87%E9%A2%98-1)\n```\n[ignored](missing.md)\n```\n', self.base[:7])
        (self.root / 'docs/assets').mkdir()
        with (self.root / 'docs/02-other.md').open('a') as handle:
            handle.write('[directory](assets/)\n')
        self.assertEqual(self.check().returncode, 0)

    def test_missing_link_and_unknown_commit_fail(self):
        self.write_doc('docs/01-guide.md', '[bad](absent.md)\n', '1234567')
        result = self.check()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('absent.md', result.stdout)
        self.assertIn('1234567', result.stdout)

    def test_changed_document_requires_new_verified_commit_and_label_only_skips_bump(self):
        self.write_doc('docs/01-guide.md', '# Changed\n', self.base[:7])
        self.commit('change')
        self.assertNotEqual(self.check('--base', self.doc_base).returncode, 0)
        self.assertEqual(self.check('--base', self.doc_base, skip=True).returncode, 0)
        self.write_doc('docs/01-guide.md', '[bad](missing.md)\n', self.base[:7])
        self.assertNotEqual(self.check('--base', self.doc_base, skip=True).returncode, 0)

    def test_unknown_base_is_error_and_new_document_is_valid(self):
        self.write_doc('docs/02-new.md', '# New\n', self.doc_base[:7])
        self.commit('new')
        self.assertEqual(self.check('--base', self.doc_base).returncode, 0)
        self.assertNotEqual(self.check('--base', 'nonexistent-base').returncode, 0)

    def test_renamed_document_still_requires_bump(self):
        self.git('mv', 'docs/01-guide.md', 'docs/02-guide.md')
        self.commit('rename')
        self.assertNotEqual(self.check('--base', self.doc_base).returncode, 0)

    def test_diagram_pair_and_changed_source(self):
        assets = self.root / 'docs/assets'
        assets.mkdir()
        (assets / '01-graph.mmd').write_text('graph TD\n')
        self.assertNotEqual(self.check().returncode, 0)
        (assets / '01-graph.svg').write_text('<svg/>')
        self.commit('pair')
        before = self.git('rev-parse', 'HEAD')
        (assets / '01-graph.mmd').write_text('graph LR\n')
        self.commit('source')
        self.assertNotEqual(self.check('--base', before).returncode, 0)

    def test_plan_directory_excluded_and_long_document_only_warns(self):
        self.write_doc('docs/01-guide.md', '# A\n' + 'x\n' * 801, self.base[:7])
        plan = self.root / 'docs/superpowers/plans/work.md'
        plan.parent.mkdir(parents=True)
        plan.write_text('draft')
        result = self.check()
        self.assertEqual(result.returncode, 0)
        self.assertIn('warning', result.stdout.lower())


if __name__ == '__main__':
    unittest.main()
