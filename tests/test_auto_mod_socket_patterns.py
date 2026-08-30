from __future__ import annotations

import sys
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from auto_mod.gim_editor import EditFeedback, _bind_socket_objects
from filefinder.core.paths import ArchiveSource, ParsedInput


class FakeAssetIndex:
    def __init__(self, existing_paths: set[str]) -> None:
        self.existing_paths = existing_paths

    def exists(self, raw_path: str) -> bool:
        return raw_path in self.existing_paths


class AutoModSocketPatternTests(unittest.TestCase):
    def test_binds_guajian_skin_code_part_name_socket(self) -> None:
        root = ET.fromstring(
            '<NeoX><Socket_0 Name="guajian_taizai_head" /></NeoX>'
        )
        parsed_gim = ParsedInput(
            raw_path=(
                "chr/player/dm65_survivor_m/h55_survivor_m_qiutu/"
                "separate_dir/qiutu_e_taizai/qiutu_e_taizai.gim"
            ),
            archive=ArchiveSource(
                stem="chr_player",
                prefix="chr/player",
                res_idx=Path("chr_player.idx"),
                documents_res_idx=Path("chr_player.idx"),
            ),
            normalized_path=(
                "dm65_survivor_m/h55_survivor_m_qiutu/"
                "separate_dir/qiutu_e_taizai/qiutu_e_taizai.gim"
            ),
        )
        expected_path = (
            "chr/player/dm65_survivor_m/h55_survivor_m_qiutu/"
            "separate_dir/qiutu_e_taizai/qiutu_e_taizai_head.gim"
        )
        feedback = EditFeedback()

        _bind_socket_objects(
            root,
            parsed_gim=parsed_gim,
            asset_index=FakeAssetIndex({expected_path}),
            prompt_socket_path=lambda socket_name, predicted_path: None,
            feedback=feedback,
        )

        obj = root.find("Socket_0/Object")
        self.assertIsNotNone(obj)
        self.assertEqual(obj.attrib["Name"], "guajian_taizai_head")
        self.assertEqual(obj.attrib["Uri"], expected_path)
        self.assertEqual(feedback.socket_updates, [f"guajian_taizai_head: {expected_path}"])
        self.assertEqual(feedback.skipped_sockets, [])


if __name__ == "__main__":
    unittest.main()
