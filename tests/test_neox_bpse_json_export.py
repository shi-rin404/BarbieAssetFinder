from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from filefinder.core.neox_bpse import BPSEDocument, BPSEReference, loads, to_bytes


class BPSEBinaryToJsonExportTests(unittest.TestCase):
    def test_exports_resolved_neox_bpse_object_root_by_default(self) -> None:
        document = BPSEDocument(
            magic=b"BPSETEST",
            unk0=1,
            unk1=2,
            unk2=3,
            root={
                "0": ["Head", "Body", "TypeName", "Enabled", "Items", "Ref"],
                "1": {
                    "0": {"2": "ParticleSystem", "3": True},
                    "1": [{"2": "Emitter", "4": [None, -3, "mesh"]}],
                    "5": BPSEReference(4096),
                },
            },
        )
        binary = to_bytes(document)

        exported = json.loads(loads(binary))

        self.assertEqual(exported["magic"], "4250534554455354")
        self.assertEqual(exported["unk0"], 1)
        self.assertEqual(exported["unk1"], 2)
        self.assertEqual(exported["unk2"], 3)
        self.assertEqual(
            exported["root"],
            {
                "Head": {"TypeName": "ParticleSystem", "Enabled": True},
                "Body": [{"TypeName": "Emitter", "Items": [None, -3, "mesh"]}],
                "Ref": {"__bpse_reference__": 4096},
            },
        )

    def test_can_export_raw_numeric_keys(self) -> None:
        document = BPSEDocument(
            magic=b"BPSETEST",
            unk0=0,
            unk1=0,
            unk2=0,
            root={"0": ["Name"], "1": {"0": "Root"}},
        )
        binary = to_bytes(document)

        exported = json.loads(loads(binary, resolve_string_table=False))

        self.assertEqual(exported["root"], {"0": ["Name"], "1": {"0": "Root"}})

    def test_root_only_export_uses_resolved_object_root(self) -> None:
        binary = to_bytes({"0": ["Name", "Values"], "1": {"0": "compact", "1": [1, 2, 3]}})

        exported = json.loads(loads(binary, include_header=False))

        self.assertEqual(exported, {"Name": "compact", "Values": [1, 2, 3]})


if __name__ == "__main__":
    unittest.main()
