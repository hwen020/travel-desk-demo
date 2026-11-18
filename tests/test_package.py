"""Portable archive and private-file exclusion regression checks."""
from pathlib import Path
import tempfile
import unittest
import zipfile

from scripts import package_public as package


class PublicPackageTests(unittest.TestCase):
    def test_portable_paths_extract_and_exclude_private_files(self):
        with tempfile.TemporaryDirectory(prefix="travel-desk-package-test-") as directory:
            root = Path(directory)
            for relative in package.FILES:
                target = root / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text("public source", encoding="utf-8")
            for relative in ("travel.sqlite3", "docs/internal.xlsx", "docs/company.docx", "original.pdf", "old.zip",
                             ".env", ".demo-data/session.sqlite3", "docs/POLICY_SOURCES.zh-CN.md",
                             "docs/POLICY_REVIEW.zh-CN.md", "tests/__pycache__/cache.pyc"):
                target = root / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text("PRIVATE", encoding="utf-8")
            result = package.build(root, root / "public.zip")
            with zipfile.ZipFile(result) as archive:
                self.assertTrue(all("\\" not in n for n in archive.namelist()))
                self.assertEqual(set(archive.namelist()) - {"public/", "docs/", "tests/", "scripts/", ".vscode/"}, set(package.FILES))
                self.assertTrue(all(b"PRIVATE" not in archive.read(n) for n in archive.namelist()))


if __name__ == "__main__":
    unittest.main()
