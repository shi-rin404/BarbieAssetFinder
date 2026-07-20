"""Copy extracted files into a flat mod folder with conflict handling."""

from __future__ import annotations

import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Literal

from filefinder.core.extract import ExtractionReport

CopyConflictAction = Literal["overwrite", "rename"]
ConflictResolver = Callable[[Path, Path], CopyConflictAction]


@dataclass(frozen=True)
class ModCopyResult:
    copied: int
    overwritten: int
    renamed: int


def sanitize_prefix_part(value: str) -> str:
    sanitized = re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("._-")
    return sanitized or "asset"


def unique_path(path: Path) -> Path:
    if not path.exists():
        return path

    stem = path.stem
    suffix = path.suffix
    for index in range(2, 10000):
        candidate = path.with_name(f"{stem}_{index}{suffix}")
        if not candidate.exists():
            return candidate
    raise FileExistsError(f"Could not find an available filename for {path}")


def prefixed_target_path(output_path: Path, output_root: Path, mod_folder: Path) -> Path:
    try:
        relative_path = output_path.resolve(strict=False).relative_to(
            output_root.resolve(strict=False)
        )
    except ValueError:
        relative_path = output_path.name

    if isinstance(relative_path, str):
        prefix = sanitize_prefix_part(output_path.stem)
    else:
        parent_parts = [sanitize_prefix_part(part) for part in relative_path.parent.parts]
        prefix = "_".join(parent_parts) if parent_parts else sanitize_prefix_part(output_path.stem)

    return unique_path(mod_folder / f"{prefix}_{output_path.name}")


def copy_report_to_mod_folder(
    report: ExtractionReport,
    *,
    output_root: Path,
    mod_folder: Path,
    resolve_conflict: ConflictResolver,
) -> ModCopyResult:
    mod_folder.mkdir(parents=True, exist_ok=True)
    copied = 0
    overwritten = 0
    renamed = 0

    for item in report.written:
        target = mod_folder / item.output_path.name
        if target.exists():
            renamed_target = prefixed_target_path(item.output_path, output_root, mod_folder)
            action = resolve_conflict(target, renamed_target)
            if action == "rename":
                target = renamed_target
                renamed += 1
            else:
                overwritten += 1

        shutil.copy2(item.output_path, target)
        copied += 1

    return ModCopyResult(copied=copied, overwritten=overwritten, renamed=renamed)

