import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from tools.build_release import PATTERNS, build, inspected_files


class ReleaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "safe.txt").write_text("safe", encoding="utf-8")

    def test_allowlist_excludes_private_files_and_git(self):
        (self.root / "account.json").write_text("private", encoding="utf-8")
        (self.root / ".git").mkdir()
        with patch("tools.build_release.FILES", ("safe.txt",)):
            result = build(self.root)
        self.assertEqual(result["file_count"], 1)
        with zipfile.ZipFile(self.root / "dist/position-desk-source.zip") as archive:
            self.assertEqual(archive.namelist(), ["position-desk/safe.txt"])

    def test_no_partial_release_when_a_required_file_is_missing(self):
        with patch("tools.build_release.FILES", ("safe.txt", "missing.txt")):
            with self.assertRaises(ValueError):
                build(self.root)
        self.assertFalse((self.root / "dist").exists())

    def test_literal_credentials_are_rejected_without_echo(self):
        credential = "ghp_" + "x"*30
        (self.root / "safe.txt").write_text(credential, encoding="utf-8")
        with patch("tools.build_release.FILES", ("safe.txt",)):
            with self.assertRaises(ValueError) as caught:
                inspected_files(self.root)
        self.assertNotIn(credential, str(caught.exception))

    def test_personal_path_is_rejected(self):
        path = "C:" + chr(92) + "Users" + chr(92) + "private-user" + chr(92) + "file.txt"
        (self.root / "safe.txt").write_text(path, encoding="utf-8")
        with patch("tools.build_release.FILES", ("safe.txt",)):
            with self.assertRaises(ValueError):
                inspected_files(self.root)

    def test_build_is_reproducible(self):
        with patch("tools.build_release.FILES", ("safe.txt",)):
            first, second = build(self.root), build(self.root)
        self.assertEqual(first["archive_sha256"], second["archive_sha256"])


if __name__ == "__main__":
    unittest.main()
