#!/usr/bin/env python3
"""CLI entry point for FileFinderV2."""

from __future__ import annotations

import argparse
import logging
import sys
import tkinter as tk
import time
from pathlib import Path
from tkinter import filedialog

from filefinder.core.extract import extract_assets
from filefinder.core.memory import resolve_game_root
from filefinder.core.mod_copy import copy_report_to_mod_folder
from filefinder.core.output import CliOutput
from filefinder.core.paths import discover_archives, parse_asset_path
from filefinder.core.tracking import extract_assets_with_tracking


FILE_TRACKING_OPTIONS = ("Mesh", "Texture", "GIM", "MTL", "MTG", "STB")
TEXTURE_TRACKING_OPTIONS = ("Diffuse", "Normal", "Metal", "Grab All")


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Resolve prefixed game asset paths to Hash128 and extract them from IDX/WPK archives."
    )
    parser.add_argument(
        "paths",
        nargs="*",
        help="Asset paths with archive prefix, for example chr/player/path/to/file.gim",
    )
    parser.add_argument(
        "--game-root",
        type=Path,
        help="Game root that contains res and Documents/res. Overrides user/memory.json when provided.",
    )
    parser.add_argument(
        "--output-folder",
        dest="output_root",
        type=Path,
        default=Path("outputs"),
        help="Directory where extracted files are written",
    )
    parser.add_argument(
        "--raw",
        action="store_true",
        help="Write raw payload bytes without stage1 or nested payload decoding",
    )
    parser.add_argument(
        "--list-archives",
        action="store_true",
        help="List archive prefixes discovered in both res folders and exit",
    )
    parser.add_argument(
        "--track",
        nargs="+",
        metavar="TYPE",
        help="Enable file tracking for selected types: Mesh Texture GIM MTL MTG STB",
    )
    parser.add_argument(
        "--track-texture",
        nargs="+",
        metavar="TYPE",
        help="Texture tracking types: Diffuse Normal Metal grab-all",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="Enable debug logging")
    return parser.parse_args(argv)


def ask_yes_no(output: CliOutput, question: str) -> bool:
    while True:
        answer = output.console.input(f"[bold cyan]{question}[/bold cyan] [Yes/No]: ").strip().lower()
        if answer in {"yes", "y"}:
            return True
        if answer in {"no", "n"}:
            return False
        output.console.print("[yellow]Please answer Yes or No.[/yellow]")


def choose_mod_folder() -> Path:
    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    try:
        selected = filedialog.askdirectory(title="Select Folder")
    finally:
        root.destroy()

    if not selected:
        raise RuntimeError("Folder selection was cancelled")
    return Path(selected)


def ask_copy_conflict(output: CliOutput, target: Path, renamed_target: Path) -> str:
    output.console.print(
        f"[yellow]File already exists:[/yellow] {target}\n"
        f"[cyan]Rename option:[/cyan] {renamed_target.name}"
    )
    while True:
        answer = output.console.input("[bold cyan]Choose[/bold cyan] [Overwrite/Rename]: ").strip().lower()
        if answer in {"overwrite", "o"}:
            return "overwrite"
        if answer in {"rename", "r"}:
            return "rename"
        output.console.print("[yellow]Please answer Overwrite or Rename.[/yellow]")


def ask_indexed_options(output: CliOutput, title: str, options: tuple[str, ...]) -> set[str]:
    output.console.print(f"\n[bold]{title}[/bold]")
    for index, option in enumerate(options, start=1):
        output.console.print(f"  [cyan]{index}[/cyan] {option}")

    while True:
        answer = output.console.input("[bold cyan]Select indexes[/bold cyan] [space-separated]: ").strip()
        if not answer:
            return set()

        selected: set[str] = set()
        invalid: list[str] = []
        for part in answer.split():
            if not part.isdigit():
                invalid.append(part)
                continue
            option_index = int(part)
            if option_index < 1 or option_index > len(options):
                invalid.append(part)
                continue
            selected.add(options[option_index - 1])

        if invalid:
            output.console.print(f"[yellow]Invalid index value(s): {', '.join(invalid)}[/yellow]")
            continue
        return selected


def ask_file_tracking_options(output: CliOutput) -> tuple[set[str], set[str]]:
    if not ask_yes_no(output, "Use file tracking?"):
        return set(), set()

    file_tracking = ask_indexed_options(output, "File Tracking", FILE_TRACKING_OPTIONS)
    texture_tracking: set[str] = set()
    if "Texture" in file_tracking:
        texture_tracking = ask_indexed_options(output, "Texture Tracking", TEXTURE_TRACKING_OPTIONS)
        if not texture_tracking:
            output.console.print("[yellow]No texture type selected. Texture tracking will be skipped.[/yellow]")
            file_tracking.discard("Texture")
    return file_tracking, texture_tracking


def tracking_options_from_args(args: argparse.Namespace) -> tuple[set[str], set[str]]:
    file_tracking = parse_option_tokens(args.track or [], FILE_TRACKING_OPTIONS, "file tracking")
    texture_tracking = parse_option_tokens(
        args.track_texture or [],
        TEXTURE_TRACKING_OPTIONS,
        "texture tracking",
    )
    if texture_tracking:
        file_tracking.add("Texture")
    if "Texture" in file_tracking and not texture_tracking:
        texture_tracking.add("Grab All")
    return file_tracking, texture_tracking


def parse_option_tokens(tokens: list[str], valid_options: tuple[str, ...], label: str) -> set[str]:
    normalized_options = {_option_key(option): option for option in valid_options}
    selected: set[str] = set()
    index = 0
    while index < len(tokens):
        token = tokens[index]
        combined = token
        if index + 1 < len(tokens):
            two_word_key = _option_key(f"{token} {tokens[index + 1]}")
            if two_word_key in normalized_options:
                selected.add(normalized_options[two_word_key])
                index += 2
                continue

        key = _option_key(combined)
        if key not in normalized_options:
            valid = ", ".join(valid_options)
            raise ValueError(f"Invalid {label} option {token!r}. Valid options: {valid}")
        selected.add(normalized_options[key])
        index += 1
    return selected


def _option_key(value: str) -> str:
    return value.replace("-", " ").replace("_", " ").replace(",", " ").lower().replace(" ", "")


def split_input_paths(value: str) -> list[str]:
    return [line.strip() for line in value.splitlines() if line.strip()]


def flatten_input_paths(values: list[str]) -> list[str]:
    paths: list[str] = []
    for value in values:
        split_paths = split_input_paths(value)
        paths.extend(split_paths if split_paths else [value.strip()])
    return [path for path in paths if path]


def read_interactive_paths(prompt: str) -> tuple[str, list[str]]:
    """Read console input; Enter adds paths, Tab starts search."""
    if sys.platform.startswith("win") and sys.stdin.isatty():
        import msvcrt

        print(prompt, end="", flush=True)
        chars: list[str] = []
        lines: list[str] = []
        action = "add"

        def add_current_line() -> None:
            line = "".join(chars).strip()
            if line:
                lines.append(line)
            chars.clear()

        def read_char(char: str) -> bool:
            nonlocal action
            if char in ("\x00", "\xe0"):
                if msvcrt.kbhit():
                    msvcrt.getwch()
                return False
            if char in ("\r", "\n"):
                add_current_line()
                print()
                return True
            if char == "\t":
                add_current_line()
                action = "search"
                print()
                return True
            if char == "\x03":
                raise KeyboardInterrupt
            if char == "\b":
                if chars:
                    chars.pop()
                    print("\b \b", end="", flush=True)
                return False
            chars.append(char)
            print(char, end="", flush=True)
            return False

        while True:
            char = msvcrt.getwch()
            finished_line = read_char(char)
            if not finished_line:
                continue

            # Multi-line paste arrives in the console input buffer immediately after
            # the first newline. Drain it so one paste can add the full path list.
            time.sleep(0.03)
            while msvcrt.kbhit():
                read_char(msvcrt.getwch())
                time.sleep(0.005)
            if chars and lines:
                add_current_line()
                print()
            return action, lines

    value = input(prompt)
    paths = split_input_paths(value)
    return ("search", []) if not paths else ("add", paths)


def interactive_path_queue(game_root: Path, output: CliOutput) -> list[str]:
    archives = discover_archives(game_root)
    queued_paths: list[str] = []
    output.console.print(
        "\n[bold]Path input mode[/bold]\n"
        "[cyan]Enter[/cyan]: Add the path into queue    "
        "[cyan]Tab[/cyan]: Search & Decompress\n"
    )

    while True:
        action, raw_paths = read_interactive_paths("Asset path: ")
        for raw_path in raw_paths:
            try:
                parsed = parse_asset_path(raw_path, archives)
            except Exception as exc:
                output.error(str(exc))
                continue

            if raw_path in queued_paths:
                output.console.print("[yellow]Path is already in the queue.[/yellow]")
            else:
                queued_paths.append(raw_path)
                output.console.print(
                    f"[green]Added[/green] {parsed.archive.prefix} / {parsed.normalized_path} "
                    f"([bold]{len(queued_paths)}[/bold] queued)"
                )

        if action == "search":
            if queued_paths:
                return queued_paths
            output.console.print("[yellow]Queue is empty. Add at least one path first.[/yellow]")


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    output = CliOutput()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s: %(message)s",
    )

    try:
        game_root = resolve_game_root(args.game_root)

        if args.list_archives:
            archives = discover_archives(game_root)
            output.archives(archives, game_root)
            return 0

        mod_folder: Path | None = None
        if ask_yes_no(output, "Send decompressed files into a selected folder?"):
            mod_folder = choose_mod_folder()
            output.console.print(f"[green]Selected folder:[/green] {mod_folder}")

        raw_paths = flatten_input_paths(args.paths) if args.paths else interactive_path_queue(game_root, output)
        if args.track or args.track_texture:
            file_tracking, texture_tracking = tracking_options_from_args(args)
        elif args.paths:
            file_tracking, texture_tracking = set(), set()
        else:
            file_tracking, texture_tracking = ask_file_tracking_options(output)

        if file_tracking:
            report = extract_assets_with_tracking(
                game_root,
                raw_paths,
                output_root=args.output_root,
                file_types=file_tracking,
                texture_types=texture_tracking,
                decode=not args.raw,
            )
        else:
            report = extract_assets(
                game_root,
                raw_paths,
                output_root=args.output_root,
                decode=not args.raw,
            )
        if mod_folder is not None:
            copy_result = copy_report_to_mod_folder(
                report,
                output_root=args.output_root,
                mod_folder=mod_folder,
                resolve_conflict=lambda target, renamed: ask_copy_conflict(output, target, renamed),
            )
            output.console.print(
                f"[green]Copied {copy_result.copied} file(s) into selected folder.[/green] "
                f"Overwritten: {copy_result.overwritten}, Renamed: {copy_result.renamed}"
            )
    except Exception as exc:
        output.error(str(exc))
        return 1

    output.extraction_report(report)

    if report.ok:
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
