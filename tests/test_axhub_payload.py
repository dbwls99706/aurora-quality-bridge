"""Local-only packaging regressions. No credentials, uploads or platform calls."""
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
from build_axhub_payload import ROOT_FILES, STATIC_FILES, build, inventory


class PayloadTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / 'source'
        self.root.mkdir()
        for name in ROOT_FILES:
            (self.root / name).write_text('reviewed source\n')
        (self.root / 'static').mkdir()
        for name in STATIC_FILES:
            (self.root / 'static' / name).write_text('reviewed asset\n')
        (self.root / 'data/original').mkdir(parents=True)
        data = b'simulation fixture\n'
        (self.root / 'data/original/example.csv').write_bytes(data)
        (self.root / 'evidence').mkdir()
        self.manifest = self.root / 'evidence/source-manifest.json'
        self.manifest.write_text(json.dumps([{'file': 'example.csv', 'bytes': len(data),
                                             'sha256': hashlib.sha256(data).hexdigest()}]))
        self.output = Path(self.tmp.name) / 'payload.zip'

    def test_payload_is_exact_and_excludes_unrelated_sensitive_state(self):
        for name in ('.env', 'local_secret.py', 'auth.json', 'runtime.sqlite3'):
            (self.root / name).write_text('must not be read or uploaded')
        result = build(self.root, self.output)
        with zipfile.ZipFile(self.output) as archive:
            self.assertEqual(set(archive.namelist()), set(ROOT_FILES)
                             | {'static/' + n for n in STATIC_FILES}
                             | {'data/original/example.csv'})
        self.assertFalse(result['uploaded'])
        self.assertFalse(result['deployed'])

    def test_build_is_deterministic(self):
        first = build(self.root, self.output)
        second = build(self.root, self.output.with_name('second.zip'))
        self.assertEqual(first['sha256'], second['sha256'])

    def test_modified_original_rejected_before_writing(self):
        (self.root / 'data/original/example.csv').write_text('changed')
        with self.assertRaisesRegex(ValueError, 'fixture changed'):
            build(self.root, self.output)
        self.assertFalse(self.output.exists())

    def test_unknown_copy_tree_files_rejected(self):
        for relative in ('static/.env', 'data/original/unreviewed.txt'):
            with self.subTest(relative=relative):
                path = self.root / relative
                path.write_text('unknown')
                with self.assertRaisesRegex(ValueError, 'Unreviewed'):
                    inventory(self.root)
                path.unlink()

    def test_symlink_and_parent_symlink_rejected(self):
        path = self.root / 'backend.py'
        path.unlink()
        path.symlink_to(self.root / 'persistence.py')
        with self.assertRaisesRegex(ValueError, 'Symlinks'):
            inventory(self.root)
        path.unlink()
        path.write_text('reviewed source')
        (self.root / 'static').rename(self.root / 'assets')
        (self.root / 'static').symlink_to(self.root / 'assets', target_is_directory=True)
        with self.assertRaisesRegex(ValueError, 'Symlinks'):
            inventory(self.root)

    def test_path_traversal_and_duplicate_manifest_rejected(self):
        entries = json.loads(self.manifest.read_text())
        self.manifest.write_text(json.dumps(entries + entries))
        with self.assertRaisesRegex(ValueError, 'Duplicate'):
            inventory(self.root)
        entries[0]['file'] = '../../backend.py'
        self.manifest.write_text(json.dumps(entries))
        with self.assertRaisesRegex(ValueError, 'Unsafe'):
            inventory(self.root)

    def test_secret_marker_rejected_without_printing_value(self):
        value = 'sk-proj-' + 'notarealcredential' * 3
        (self.root / 'backend.py').write_text(value)
        with self.assertRaisesRegex(ValueError, 'Credential-like') as error:
            inventory(self.root)
        self.assertNotIn(value, str(error.exception))

    def test_existing_output_is_never_overwritten(self):
        self.output.write_text('existing artifact')
        with self.assertRaises(FileExistsError):
            build(self.root, self.output)
        self.assertEqual(self.output.read_text(), 'existing artifact')

    def test_special_file_rejected_without_blocking(self):
        import os
        path = self.root / 'backend.py'
        path.unlink()
        os.mkfifo(path)
        with self.assertRaisesRegex(ValueError, 'not a regular file'):
            inventory(self.root)


if __name__ == '__main__':
    unittest.main()
