"""Persistent CLI memory for local game configuration."""

from __future__ import annotations

import json
import string
import base64
import tkinter as tk
from pathlib import Path
from tkinter import filedialog

from rich.console import Console
from rich.padding import Padding
from rich.panel import Panel

MEMORY_PATH = Path(__file__).resolve().parents[2] / "user" / "memory.json"
DEFAULT_MEMORY = {"game_root": ""}
DEFAULT_EXECUTABLE_RELATIVE_PATH = Path(
    base64.decodebytes(
        b"TG9hZGluZyBCYXkgR2FtZXMvSWRlbnRpdHkgVi9kd3JnLmV4ZQ=="
    ).decode("utf-8")
)
EXECUTABLE_NAME = base64.decodebytes(b"ZHdyZy5leGU=").decode("utf-8")

def ensure_memory_file() -> None:
    MEMORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    if not MEMORY_PATH.exists():
        MEMORY_PATH.write_text(
            json.dumps(DEFAULT_MEMORY, indent=2) + "\n",
            encoding="utf-8",
        )


def load_memory() -> dict[str, str]:
    ensure_memory_file()
    try:
        data = json.loads(MEMORY_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        data = DEFAULT_MEMORY.copy()

    if not isinstance(data, dict):
        data = DEFAULT_MEMORY.copy()
    game_root = data.get("game_root", "")
    if not isinstance(game_root, str):
        game_root = ""
    return {"game_root": game_root}


def save_game_root(game_root: Path) -> None:
    ensure_memory_file()
    data = load_memory()
    data["game_root"] = str(game_root)
    MEMORY_PATH.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def find_default_game_executable() -> Path | None:
    """Search fixed drive roots for the default game executable path."""
    for drive_letter in string.ascii_uppercase:
        candidate = Path(f"{drive_letter}:\\") / DEFAULT_EXECUTABLE_RELATIVE_PATH
        if candidate.is_file():
            return candidate
    return None


def confirm_default_game_executable(executable_path: Path) -> bool:
    """Ask whether an automatically discovered executable should be used."""
    console = Console()
    console.print(
        Padding(
            Panel(
                f"[bold]Found game executable:[/bold]\n[cyan]{executable_path}[/cyan]",
                title="Game Executable",
                border_style="cyan",
                padding=(1, 2),
            ),
            (1, 2, 0, 2),
        )
    )

    while True:
        try:
            answer = console.input(
                "  [bold cyan]Use this path?[/bold cyan] [Yes/No]: "
            ).strip().lower()
        except EOFError:
            return False

        if answer in {"yes", "y"}:
            return True
        if answer in {"no", "n"}:
            return False
        console.print("  [yellow]Please answer Yes or No.[/yellow]")


def choose_game_executable() -> Path:
    """Ask the user to select dwrg.exe and return its parent directory."""
    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    try:
        selected = filedialog.askopenfilename(
            title="Game Executable",
            filetypes=[(EXECUTABLE_NAME, EXECUTABLE_NAME)],
        )
    finally:
        root.destroy()

    if not selected:
        raise RuntimeError("Game executable selection was cancelled")

    executable_path = Path(selected)
    if executable_path.name.lower() != EXECUTABLE_NAME:
        raise ValueError(f"Selected executable must be {EXECUTABLE_NAME}")
    return executable_path.parent


def resolve_game_root(cli_game_root: Path | None) -> Path:
    """Resolve game root from CLI override, memory, or file dialog."""
    if cli_game_root is not None:
        return cli_game_root.resolve()

    memory = load_memory()
    stored_game_root = memory["game_root"].strip()
    if stored_game_root:
        return Path(stored_game_root)

    default_executable = find_default_game_executable()
    if default_executable is not None and confirm_default_game_executable(default_executable):
        game_root = default_executable.parent
    else:
        game_root = choose_game_executable()

    save_game_root(game_root)
    return game_root
