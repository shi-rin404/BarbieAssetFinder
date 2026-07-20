#!/usr/bin/env python3
"""PySide6 entry point for Auto Mod."""

from __future__ import annotations

import re
import sys
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from auto_mod.workflow import AutoModPrompts, AutoModResult, run_auto_mod
from filefinder.core.memory import (
    EXECUTABLE_NAME,
    find_default_game_executable,
    load_memory,
    save_game_root,
)
from filefinder.core.paths import discover_archives, parse_asset_path


APP_DIR = Path(__file__).resolve().parent


def ask_yes_no(
    parent: QWidget,
    title: str,
    text: str,
    *,
    default_yes: bool = True,
    icon: QMessageBox.Icon = QMessageBox.Question,
) -> bool:
    dialog = QMessageBox(parent)
    dialog.setWindowTitle(title)
    dialog.setIcon(icon)
    dialog.setText(text)
    no_button = dialog.addButton("No", QMessageBox.ActionRole)
    yes_button = dialog.addButton("Yes", QMessageBox.ActionRole)
    dialog.setDefaultButton(yes_button if default_yes else no_button)
    dialog.exec()
    return dialog.clickedButton() == yes_button


class GuiPrompts(AutoModPrompts):
    def __init__(self, parent: QWidget) -> None:
        self.parent = parent
        self.current_gim_path = ""
        self.mod_name: str | None = None

    def set_current_gim(self, gim_path: str) -> None:
        self.current_gim_path = gim_path

    def context_text(self) -> str:
        if not self.current_gim_path:
            return ""
        return f"\n\nGIM:\n{self.current_gim_path}"

    def ask_dependency_path(self, tag_name: str, raw_value: str, extension: str) -> str | None:
        displayed = dependency_display_value(raw_value)
        QMessageBox.information(
            self.parent,
            "Dependency Path",
            f"Could not resolve {tag_name} from:\n\n{displayed}{self.context_text()}",
        )
        value, accepted = QInputDialog.getText(
            self.parent,
            "Dependency Path",
            f"Enter full archive path for skeleton or animconfig:{self.context_text()}",
            QLineEdit.Normal,
            "",
        )
        if not accepted:
            return None
        value = value.strip()
        return value or None

    def ask_socket_path(self, socket_name: str, predicted_path: str) -> str | None:
        QMessageBox.information(
            self.parent,
            "Socket Object",
            (
                "Socket object was not found in archives."
                f"\n\nSocket: {socket_name}\nPredicted: {predicted_path}{self.context_text()}"
            ),
        )
        value, accepted = QInputDialog.getText(
            self.parent,
            "Socket Object",
            f"Enter object GIM archive path, or leave empty to skip:{self.context_text()}",
            QLineEdit.Normal,
            "",
        )
        if not accepted:
            return None
        value = value.strip()
        return value or None

    def ask_mod_name(self) -> str | None:
        if self.mod_name is not None:
            return self.mod_name
        value, accepted = QInputDialog.getText(
            self.parent,
            "Mod Name",
            f"Enter mod name:{self.context_text()}",
            QLineEdit.Normal,
            "",
        )
        if not accepted:
            return None
        value = value.strip()
        self.mod_name = value or None
        return self.mod_name

    def ask_mod_key(self, archive_stem: str, default_key: str) -> str | None:
        if self.ask_use_default_mod_key(archive_stem, default_key):
            return default_key

        return self.ask_custom_mod_key(default_key, "Enter custom mod key:")

    def ask_mod_key_conflict(self, conflicting_key: str, existing_value: str, new_value: str) -> str | None:
        QMessageBox.warning(
            self.parent,
            "Mod Key Conflict",
            (
                f"Mod key '{conflicting_key}' already exists in mod.json."
                f"\n\nExisting value:\n{existing_value}\n\nNew value:\n{new_value}{self.context_text()}"
            ),
        )
        return self.ask_custom_mod_key(conflicting_key, "Enter a different mod key:")

    def ask_use_default_mod_key(self, archive_stem: str, default_key: str) -> bool:
        return ask_yes_no(
            self.parent,
            "Mod Key",
            f"Use '{default_key}' as mod key for {archive_stem}?{self.context_text()}",
            default_yes=True,
        )

    def ask_custom_mod_key(self, default_key: str, label: str) -> str | None:
        value, accepted = QInputDialog.getText(
            self.parent,
            "Mod Key",
            f"{label}{self.context_text()}",
            QLineEdit.Normal,
            default_key,
        )
        if not accepted:
            return None
        value = value.strip()
        return value or None


class AutoModWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Auto Mod")
        self.resize(900, 620)
        self.game_root: Path | None = None
        self.output_folder: Path | None = None

        self._build_ui()
        self._apply_theme()
        self._resolve_game_root()

    def _build_ui(self) -> None:
        central = QWidget()
        root = QVBoxLayout(central)
        root.setContentsMargins(30, 28, 30, 28)
        root.setSpacing(20)
        self.setCentralWidget(central)

        title = QLabel("Auto Mod")
        title.setObjectName("AppTitle")
        title.setAlignment(Qt.AlignCenter)
        root.addWidget(title)

        frame = QFrame()
        frame.setObjectName("FormFrame")
        form = QGridLayout(frame)
        form.setContentsMargins(14, 14, 14, 14)
        form.setHorizontalSpacing(12)
        form.setVerticalSpacing(12)
        form.setColumnStretch(0, 1)

        self.output_label = QLabel("Output path: not selected")
        self.output_label.setObjectName("PathLabel")
        self.browse_button = QPushButton("Browse")
        self.browse_button.setObjectName("SubtleButton")
        self.gim_input = QLineEdit()
        self.gim_input.setPlaceholderText("GIM archive path, for example chr/player/.../file.gim")
        self.add_button = QPushButton("Add")
        self.add_button.setObjectName("PrimaryButton")
        self.auto_mod_button = QPushButton("Auto Mod")
        self.auto_mod_button.setObjectName("AutoModButton")

        form.addWidget(self.output_label, 0, 0)
        form.addWidget(self.browse_button, 0, 1)
        form.addWidget(self.gim_input, 1, 0)
        form.addWidget(self.add_button, 1, 1)
        root.addWidget(frame)

        self.queue_table = QTableWidget(0, 2)
        self.queue_table.setHorizontalHeaderLabels(["Archive Name", "File Path"])
        self.queue_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.queue_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.queue_table.verticalHeader().setVisible(False)
        self.queue_table.setAlternatingRowColors(True)
        self.queue_table.setSelectionBehavior(QTableWidget.SelectRows)
        self.queue_table.setEditTriggers(QTableWidget.NoEditTriggers)
        root.addWidget(self.queue_table, 1)

        status_frame = QFrame()
        status_frame.setObjectName("StatusFrame")
        status_layout = QHBoxLayout(status_frame)
        status_layout.setContentsMargins(12, 8, 12, 8)
        status_layout.setSpacing(10)
        self.status_label = QLabel("Ready")
        self.status_label.setObjectName("StatusLabel")
        status_layout.addWidget(self.status_label, 1)
        status_layout.addWidget(self.auto_mod_button, 0, Qt.AlignRight)
        root.addWidget(status_frame)

        self.browse_button.clicked.connect(self.choose_output_folder)
        self.add_button.clicked.connect(self.add_gim_path)
        self.auto_mod_button.clicked.connect(self.run_auto_mod)
        self.gim_input.returnPressed.connect(self.add_gim_path)

    def _resolve_game_root(self) -> None:
        memory = load_memory()
        stored = memory.get("game_root", "").strip()
        if stored:
            self.game_root = Path(stored)
            return

        default_executable = find_default_game_executable()
        if default_executable is not None:
            if ask_yes_no(
                self,
                "Game Executable",
                f"Found game executable:\n\n{default_executable}\n\nUse this path?",
                default_yes=True,
            ):
                self.game_root = default_executable.parent
                save_game_root(self.game_root)
                return
        self.change_game_root()

    def change_game_root(self) -> None:
        selected, _filter = QFileDialog.getOpenFileName(
            self,
            "Game Executable",
            "",
            f"{EXECUTABLE_NAME} ({EXECUTABLE_NAME})",
        )
        if not selected:
            return
        executable_path = Path(selected)
        if executable_path.name.lower() != EXECUTABLE_NAME:
            QMessageBox.warning(self, "Game Executable", f"Selected executable must be {EXECUTABLE_NAME}")
            return
        self.game_root = executable_path.parent
        save_game_root(self.game_root)

    def choose_output_folder(self) -> bool:
        if self.game_root is None:
            self._resolve_game_root()
            if self.game_root is None:
                return False

        mod_root = self.game_root / "Documents" / "res" / "mod"
        mod_root.mkdir(parents=True, exist_ok=True)
        while True:
            selected = QFileDialog.getExistingDirectory(
                self,
                "Select Output Folder",
                str(mod_root),
            )
            if not selected:
                return False
            folder = Path(selected).resolve()
            try:
                folder.relative_to(mod_root.resolve())
            except ValueError:
                QMessageBox.warning(
                    self,
                    "Output Folder",
                    f"Output folder must be inside:\n\n{mod_root}",
                )
                continue
            self.output_folder = folder
            self.output_label.setText(f"Output path: {folder}")
            self.output_label.setToolTip(str(folder))
            return True

    def add_gim_path(self) -> None:
        gim_path = self.gim_input.text().strip()
        if not gim_path:
            return
        if self.queue_gim_path(gim_path):
            self.gim_input.clear()
            self.status_label.setText(f"Queued {self.queue_table.rowCount()} GIM file(s)")

    def queue_gim_path(self, gim_path: str) -> bool:
        if self.game_root is None:
            QMessageBox.warning(self, "Game Root", "Select a game executable first.")
            return False

        try:
            archives = discover_archives(self.game_root)
            parsed = parse_asset_path(gim_path, archives)
            if not parsed.normalized_path.lower().endswith(".gim"):
                QMessageBox.warning(self, "GIM Path", "Queued asset must be a .gim file.")
                return False
            if self.has_queued_gim(gim_path):
                self.status_label.setText("GIM path is already in the queue")
                return True
            self.add_queue_row(gim_path, parsed.archive.stem, parsed.normalized_path)
            return True
        except Exception as exc:
            QMessageBox.critical(self, "GIM Path Failed", str(exc))
            self.status_label.setText("GIM path addition failed")
            return False

    def has_queued_gim(self, gim_path: str) -> bool:
        for row in range(self.queue_table.rowCount()):
            item = self.queue_table.item(row, 0)
            if item is not None and item.data(Qt.UserRole) == gim_path:
                return True
        return False

    def add_queue_row(self, raw_path: str, archive_name: str, normalized_path: str) -> None:
        row = self.queue_table.rowCount()
        self.queue_table.insertRow(row)
        archive_item = QTableWidgetItem(archive_name)
        archive_item.setData(Qt.UserRole, raw_path)
        archive_item.setToolTip(raw_path)
        path_item = QTableWidgetItem(normalized_path)
        path_item.setData(Qt.UserRole, raw_path)
        path_item.setToolTip("Pending auto mod")
        self.queue_table.setItem(row, 0, archive_item)
        self.queue_table.setItem(row, 1, path_item)

    def queued_gim_paths(self) -> list[str]:
        paths: list[str] = []
        for row in range(self.queue_table.rowCount()):
            item = self.queue_table.item(row, 0)
            if item is None:
                continue
            raw_path = item.data(Qt.UserRole)
            if isinstance(raw_path, str) and raw_path:
                paths.append(raw_path)
        return paths

    def clear_queue(self) -> None:
        self.queue_table.setRowCount(0)

    def run_auto_mod(self) -> None:
        if self.game_root is None:
            self._resolve_game_root()
            if self.game_root is None:
                QMessageBox.warning(self, "Game Root", "Select a game executable first.")
                return

        if self.output_folder is None and not self.choose_output_folder():
            return

        pending_gim_path = self.gim_input.text().strip()
        if pending_gim_path:
            if not self.queue_gim_path(pending_gim_path):
                return
            self.gim_input.clear()

        gim_paths = self.queued_gim_paths()
        if not gim_paths:
            QMessageBox.information(self, "Auto Mod", "Add at least one GIM path before running Auto Mod.")
            return

        self.set_busy(True)
        prompts = GuiPrompts(self)
        results: list[tuple[str, AutoModResult]] = []
        errors: list[str] = []
        try:
            for gim_path in gim_paths:
                prompts.set_current_gim(gim_path)
                try:
                    result = run_auto_mod(
                        game_root=self.game_root,
                        output_folder=self.output_folder,
                        gim_path=gim_path,
                        prompts=prompts,
                    )
                    results.append((gim_path, result))
                except Exception as exc:
                    errors.append(f"{gim_path}: {exc}")

            if results:
                self.status_label.setText(f"Auto modded {len(results)} GIM file(s)")
                QMessageBox.information(self, "Auto Mod", batch_result_message(results))
            if errors:
                self.status_label.setText(f"Auto Mod completed with {len(errors)} error(s)")
                QMessageBox.critical(self, "Auto Mod Errors", "\n\n".join(errors[:8]))
            if results and not errors:
                self.clear_queue()
        finally:
            self.set_busy(False)

    def set_busy(self, busy: bool) -> None:
        self.browse_button.setEnabled(not busy)
        self.add_button.setEnabled(not busy)
        self.gim_input.setEnabled(not busy)
        self.queue_table.setEnabled(not busy)
        self.auto_mod_button.setEnabled(not busy)
        QApplication.setOverrideCursor(Qt.WaitCursor) if busy else QApplication.restoreOverrideCursor()

    def _apply_theme(self) -> None:
        self.setStyleSheet(
            """
            QMainWindow, QWidget {
                background: #0f131a;
                color: #e6eaf2;
                font-size: 10pt;
            }
            #AppTitle {
                font-size: 24pt;
                font-weight: 700;
                color: #f6f8fb;
            }
            #FormFrame, #StatusFrame {
                background: #161b24;
                border: 1px solid #283140;
                border-radius: 8px;
            }
            #PathLabel {
                color: #b9c4d6;
                background: #161b24;
                padding: 8px 4px;
            }
            #StatusLabel {
                color: #9ecbff;
                background: #161b24;
            }
            QLineEdit {
                background: #0d1118;
                border: 1px solid #2e394a;
                border-radius: 7px;
                padding: 10px 12px;
                selection-background-color: #2f81f7;
            }
            QLineEdit:focus {
                border-color: #4f8cff;
            }
            QPushButton {
                background: #242c3a;
                border: 1px solid #334055;
                border-radius: 7px;
                padding: 9px 14px;
                color: #edf2fa;
                min-width: 112px;
            }
            QPushButton:hover {
                background: #2c3647;
            }
            QPushButton:disabled {
                color: #747d8d;
                background: #171c25;
            }
            #PrimaryButton {
                background: #2f81f7;
                border-color: #5a9cff;
                color: white;
                font-weight: 650;
                min-width: 82px;
            }
            #PrimaryButton:hover {
                background: #4d95ff;
            }
            #AutoModButton {
                background: #238636;
                border-color: #3fb950;
                color: white;
                font-weight: 650;
                min-height: 36px;
            }
            #AutoModButton:hover {
                background: #2ea043;
            }
            QTableWidget {
                background: #0d1118;
                alternate-background-color: #121823;
                border: 1px solid #283140;
                border-radius: 7px;
                gridline-color: #283140;
                selection-background-color: #244f87;
                selection-color: #ffffff;
            }
            QHeaderView::section {
                background: #1d2532;
                color: #b9c4d6;
                border: 0;
                border-right: 1px solid #303a4d;
                padding: 8px;
                font-weight: 650;
            }
            QTableWidget::item {
                padding: 6px;
            }
            """
        )

    def closeEvent(self, event) -> None:
        QApplication.restoreOverrideCursor()
        super().closeEvent(event)


def dependency_display_value(raw_value: str) -> str:
    if not raw_value:
        return "[empty]"
    return re.sub(r"\.(skeleton|animconfig)$", ".[skeleton|animconfig]", raw_value, flags=re.IGNORECASE)


def result_message(result: AutoModResult, gim_path: str) -> str:
    lines = [f"GIM:\n{gim_path}", f"\nWrote:\n{result.output_path}", f"\nManifest:\n{result.manifest_path}"]
    if result.feedback.dependency_updates:
        lines.append("\nDependency updates:")
        lines.extend(f"- {item}" for item in result.feedback.dependency_updates)
    if result.feedback.socket_updates:
        lines.append("\nSocket updates:")
        lines.extend(f"- {item}" for item in result.feedback.socket_updates[:12])
        if len(result.feedback.socket_updates) > 12:
            lines.append(f"- ... {len(result.feedback.socket_updates) - 12} more")
    if result.feedback.skipped_sockets:
        lines.append("\nSkipped sockets:")
        lines.extend(f"- {item}" for item in result.feedback.skipped_sockets[:8])
    if not result.feedback.socket_updates:
        lines.append("\nNo matching socket object bindings were found.")
    return "\n".join(lines)


def batch_result_message(results: list[tuple[str, AutoModResult]]) -> str:
    return "\n\n".join(result_message(result, gim_path) for gim_path, result in results)


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("Auto Mod")
    window = AutoModWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
