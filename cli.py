#!/usr/bin/env python3
"""CLI entry point for FileFinderV2."""

from __future__ import annotations

import argparse
import logging
import sys
import tkinter as tk
from pathlib import Path
from tkinter import filedialog

from filefinder.core.extract import extract_assets
from filefinder.core.memory import resolve_game_root
from filefinder.core.mod_copy import copy_report_to_mod_folder
from filefinder.core.output import CliOutput
from filefinder.core.paths import discover_archives, parse_asset_path


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
        "--output-root",
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


def read_interactive_line(prompt: str) -> tuple[str, str]:
    """Read one console line; Enter returns add, Tab returns search."""
    if sys.platform.startswith("win") and sys.stdin.isatty():
        import msvcrt

        print(prompt, end="", flush=True)
        chars: list[str] = []
        while True:
            char = msvcrt.getwch()
            if char in ("\x00", "\xe0"):
                msvcrt.getwch()
                continue
            if char == "\r":
                print()
                return "add", "".join(chars).strip()
            if char == "\t":
                print()
                return "search", "".join(chars).strip()
            if char == "\x03":
                raise KeyboardInterrupt
            if char == "\b":
                if chars:
                    chars.pop()
                    print("\b \b", end="", flush=True)
                continue
            if char in ("\n",):
                print()
                return "add", "".join(chars).strip()
            chars.append(char)
            print(char, end="", flush=True)

    value = input(prompt).strip()
    return ("search", "") if not value else ("add", value)


def interactive_path_queue(game_root: Path, output: CliOutput) -> list[str]:
    archives = discover_archives(game_root)
    queued_paths: list[str] = []
    output.console.print(
        "\n[bold]Path input mode[/bold]\n"
        "[cyan]Enter[/cyan]: Add the path into queue    "
        "[cyan]Tab[/cyan]: Search & Decompress\n"
    )

    while True:
        action, raw_path = read_interactive_line("Asset path: ")
        if raw_path:
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

        raw_paths = args.paths or interactive_path_queue(game_root, output)

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
