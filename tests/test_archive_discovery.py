from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from filefinder.core.paths import discover_archives


class ArchiveDiscoveryTests(unittest.TestCase):
    def test_discovers_archive_present_only_in_documents_res(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            game_root = Path(temporary_directory)
            documents_res = game_root / "Documents" / "res"
            documents_res.mkdir(parents=True)
            documents_idx = documents_res / "section.idx"
            documents_idx.touch()

            archive = discover_archives(game_root)["section"]

            self.assertEqual(archive.idx_paths, (documents_idx,))

    def test_skips_copy_removed_after_discovery(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            game_root = Path(temporary_directory)
            res = game_root / "res"
            documents_res = game_root / "Documents" / "res"
            res.mkdir(parents=True)
            documents_res.mkdir(parents=True)
            res_idx = res / "section.idx"
            documents_idx = documents_res / "section.idx"
            res_idx.touch()
            documents_idx.touch()

            archive = discover_archives(game_root)["section"]
            documents_idx.unlink()

            self.assertEqual(archive.idx_paths, (res_idx,))


if __name__ == "__main__":
    unittest.main()
