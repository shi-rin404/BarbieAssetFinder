#!/usr/bin/env python3
"""CLI entry point for FileFinderV2."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
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


@dataclass(frozen=True)
class CliRunOptions:
    file_tracking: set[str]
    texture_tracking: set[str]
    auto_decode_nx_xml: bool = True


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
        help="Do not decode binary NX-XML GIM, MTG, and MTL files when writing outputs",
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


def ask_additional_options(output: CliOutput) -> CliRunOptions:
    output.console.print(
        "\n[bold]Additional options[/bold]\n"
        "  [cyan]1[/cyan] Auto Decode NX-XML [green]enabled by default[/green]\n"
        "  [cyan]2[/cyan] File Tracking"
    )
    if not ask_yes_no(output, "Use any additional option?"):
        return CliRunOptions(file_tracking=set(), texture_tracking=set(), auto_decode_nx_xml=True)

    if sys.platform.startswith("win") and sys.stdin.isatty():
        return interactive_additional_options_menu(output)

    file_tracking = ask_indexed_options(output, "File Tracking", FILE_TRACKING_OPTIONS)
    texture_tracking: set[str] = set()
    if "Texture" in file_tracking:
        texture_tracking = ask_indexed_options(output, "Texture Tracking", TEXTURE_TRACKING_OPTIONS)
        if not texture_tracking:
            output.console.print("[yellow]No texture type selected. Texture tracking will be skipped.[/yellow]")
            file_tracking.discard("Texture")
    return CliRunOptions(
        file_tracking=file_tracking,
        texture_tracking=texture_tracking,
        auto_decode_nx_xml=True,
    )


def interactive_additional_options_menu(output: CliOutput) -> CliRunOptions:
    import ctypes
    import msvcrt

    auto_decode_nx_xml = True
    file_tracking: set[str] = set()
    texture_tracking: set[str] = set()
    menu_items = [
        ("auto", "Auto Decode NX-XML"),
        ("file:Mesh", "File Tracking: Mesh"),
        ("file:Texture", "File Tracking: Texture"),
        ("file:GIM", "File Tracking: GIM"),
        ("file:MTL", "File Tracking: MTL"),
        ("file:MTG", "File Tracking: MTG"),
        ("file:STB", "File Tracking: STB"),
        ("texture:Diffuse", "Texture Tracking: Diffuse"),
        ("texture:Normal", "Texture Tracking: Normal"),
        ("texture:Metal", "Texture Tracking: Metal"),
        ("texture:Grab All", "Texture Tracking: Grab All"),
    ]
    selected_index = 0
    shift_was_down = False

    def is_shift_down() -> bool:
        return bool(ctypes.windll.user32.GetAsyncKeyState(0x10) & 0x8000)

    def is_checked(key: str) -> bool:
        if key == "auto":
            return auto_decode_nx_xml
        if key.startswith("file:"):
            return key.split(":", 1)[1] in file_tracking
        if key.startswith("texture:"):
            return key.split(":", 1)[1] in texture_tracking
        return False

    def render() -> None:
        output.console.clear()
        output.console.print(
            "[bold]Additional Options[/bold]\n"
            "[cyan]Up/Down[/cyan]: Navigate    [cyan]Space[/cyan]: Toggle    "
            "[cyan]Shift[/cyan]: Confirm\n"
        )
        for index, (key, label) in enumerate(menu_items):
            pointer = ">" if index == selected_index else " "
            marker = "[x]" if is_checked(key) else "[ ]"
            style = "reverse" if index == selected_index else ""
            output.console.print(f"{pointer} {marker} {label}", style=style)

    def toggle(key: str) -> None:
        nonlocal auto_decode_nx_xml
        if key == "auto":
            auto_decode_nx_xml = not auto_decode_nx_xml
            return

        if key.startswith("file:"):
            option = key.split(":", 1)[1]
            if option in file_tracking:
                file_tracking.remove(option)
                if option == "Texture":
                    texture_tracking.clear()
            else:
                file_tracking.add(option)
            return

        if key.startswith("texture:"):
            option = key.split(":", 1)[1]
            file_tracking.add("Texture")
            if option == "Grab All":
                if option in texture_tracking:
                    texture_tracking.remove(option)
                else:
                    texture_tracking.clear()
                    texture_tracking.add(option)
                return
            texture_tracking.discard("Grab All")
            if option in texture_tracking:
                texture_tracking.remove(option)
            else:
                texture_tracking.add(option)

    render()
    while True:
        shift_down = is_shift_down()
        if shift_down and not shift_was_down:
            output.console.clear()
            if "Texture" in file_tracking and not texture_tracking:
                texture_tracking.add("Grab All")
            return CliRunOptions(
                file_tracking=file_tracking,
                texture_tracking=texture_tracking,
                auto_decode_nx_xml=auto_decode_nx_xml,
            )
        shift_was_down = shift_down

        if not msvcrt.kbhit():
            time.sleep(0.05)
            continue

        char = msvcrt.getwch()
        if char in ("\x00", "\xe0"):
            code = msvcrt.getwch()
            if code == "H":
                selected_index = (selected_index - 1) % len(menu_items)
            elif code == "P":
                selected_index = (selected_index + 1) % len(menu_items)
        elif char == " ":
            toggle(menu_items[selected_index][0])
        elif char in ("\r", "\n"):
            output.console.clear()
            if "Texture" in file_tracking and not texture_tracking:
                texture_tracking.add("Grab All")
            return CliRunOptions(
                file_tracking=file_tracking,
                texture_tracking=texture_tracking,
                auto_decode_nx_xml=auto_decode_nx_xml,
            )
        elif char == "\x03":
            raise KeyboardInterrupt
        render()


def tracking_options_from_args(args: argparse.Namespace) -> CliRunOptions:
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
    return CliRunOptions(
        file_tracking=file_tracking,
        texture_tracking=texture_tracking,
        auto_decode_nx_xml=not args.raw,
    )


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

        if args.paths:
            run_options = tracking_options_from_args(args)
            raw_paths = flatten_input_paths(args.paths)
        else:
            run_options = ask_additional_options(output)
            raw_paths = interactive_path_queue(game_root, output)

        if run_options.file_tracking:
            report = extract_assets_with_tracking(
                game_root,
                raw_paths,
                output_root=args.output_root,
                file_types=run_options.file_tracking,
                texture_types=run_options.texture_tracking,
                auto_decode_nx_xml=run_options.auto_decode_nx_xml,
            )
        else:
            report = extract_assets(
                game_root,
                raw_paths,
                output_root=args.output_root,
                auto_decode_nx_xml=run_options.auto_decode_nx_xml,
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
