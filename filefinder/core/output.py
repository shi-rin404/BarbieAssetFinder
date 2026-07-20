"""Formatted CLI output helpers."""

from __future__ import annotations

from pathlib import Path

from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.padding import Padding
from rich.table import Table
from rich.text import Text

from filefinder.core.extract import ExtractionReport
from filefinder.core.paths import ArchiveSource


class CliOutput:
    """Render user-facing CLI output with Rich."""

    def __init__(self) -> None:
        self.console = Console()
        self.error_console = Console(stderr=True)

    def error(self, message: str) -> None:
        self.error_console.print(
            Padding(
                Panel(
                    Text(str(message), style="bold red"),
                    title="Error",
                    border_style="red",
                    padding=(1, 2),
                ),
                (1, 2),
            )
        )

    def archives(self, archives: dict[str, ArchiveSource], game_root: Path) -> None:
        table = Table(
            title="Discovered Archives",
            box=box.ROUNDED,
            header_style="bold cyan",
            show_lines=False,
            pad_edge=True,
            expand=True,
        )
        table.add_column("Prefix", style="green", no_wrap=True)
        table.add_column("IDX File", style="white", no_wrap=True)
        table.add_column("Documents/res", justify="center", style="green", no_wrap=True)
        table.add_column("res", justify="center", style="green", no_wrap=True)

        for prefix in sorted(archives):
            archive = archives[prefix]
            table.add_row(
                prefix,
                f"{archive.stem}.idx",
                "found",
                "found",
            )

        self.console.print(
            Padding(
                Panel(
                    f"[bold]Game root:[/bold] [cyan]{game_root}[/cyan]",
                    title="FileFinderV2",
                    border_style="cyan",
                    padding=(1, 2),
                ),
                (1, 2),
            )
        )
        self.console.print(Padding(table, (0, 2, 1, 2)))

    def extraction_report(self, report: ExtractionReport) -> None:
        self.console.print(
            Padding(
                Panel(
                    self._summary_text(report),
                    title="Extraction Summary",
                    border_style="green" if report.ok else "yellow",
                    padding=(1, 2),
                ),
                (1, 2, 0, 2),
            )
        )
        self._rich_asset_details(report)

    def _summary_text(self, report: ExtractionReport) -> Text:
        text = Text()
        text.append("Resolved: ", style="bold")
        text.append(str(len(report.lookups)), style="cyan")
        text.append("    Extracted: ", style="bold")
        text.append(str(len(report.written)), style="green")
        text.append("    Missing: ", style="bold")
        text.append(str(len(report.missing)), style="red" if report.missing else "green")
        return text

    def _rich_asset_details(self, report: ExtractionReport) -> None:
        if not report.lookups:
            return

        written_by_hash = {item.hash128_hex: item for item in report.written}
        missing_hashes = {item.hash128_hex for item in report.missing}

        for index, item in enumerate(report.lookups, start=1):
            written = written_by_hash.get(item.lookup.hash128_hex)
            status = "Extracted" if written else "Missing" if item.lookup.hash128_hex in missing_hashes else "Resolved"
            status_style = "green" if status == "Extracted" else "red" if status == "Missing" else "yellow"

            grid = Table.grid(padding=(0, 2))
            grid.add_column(style="bold cyan", no_wrap=True)
            grid.add_column(ratio=1, overflow="fold")
            grid.add_row("Status", f"[{status_style}]{status}[/{status_style}]")
            grid.add_row("Archive", item.request.archive.prefix)
            grid.add_row("Path", item.request.normalized_path)
            grid.add_row("Hash128", f"[magenta]{item.lookup.hash128_hex}[/magenta]")
            grid.add_row("Lookup key", f"[cyan]0x{item.lookup.final_key:08X}[/cyan]")

            if written is not None:
                grid.add_row("Output", str(written.output_path))
                grid.add_row("Bytes", f"{written.byte_count:,}")
                grid.add_row("Source IDX", str(written.source_archive))

            self.console.print(
                Padding(
                    Panel(
                        grid,
                        title=f"Asset {index}",
                        border_style=status_style,
                        padding=(1, 2),
                    ),
                    (1, 2, 0, 2),
                )
            )
