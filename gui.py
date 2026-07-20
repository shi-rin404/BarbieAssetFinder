#!/usr/bin/env python3
"""PySide6 GUI for FileFinderV2."""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

from PySide6.QtCore import QDir, QMimeData, QModelIndex, QSortFilterProxyModel, Qt, QUrl
from PySide6.QtGui import QAction, QCloseEvent, QCursor, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QFileDialog,
    QFileSystemModel,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTreeView,
    QVBoxLayout,
    QWidget,
)

from filefinder.core.extract import ExtractionReport, extract_assets
from filefinder.core.memory import (
    find_default_game_executable,
    load_memory,
    save_game_root,
)
from filefinder.core.mod_copy import ModCopyResult, copy_report_to_mod_folder
from filefinder.core.paths import discover_archives, parse_asset_path


APP_DIR = Path(__file__).resolve().parent
OUTPUT_ROOT = APP_DIR / "outputs"
OUTPUT_ROOT_RESOLVED = OUTPUT_ROOT.resolve()


def is_output_path(path: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(OUTPUT_ROOT_RESOLVED)
    except ValueError:
        return False
    return True


def is_output_parent_path(path: Path) -> bool:
    resolved = path.resolve(strict=False)
    if resolved == OUTPUT_ROOT_RESOLVED:
        return True
    try:
        OUTPUT_ROOT_RESOLVED.relative_to(resolved)
    except ValueError:
        return False
    return True


class FileNameFilterProxy(QSortFilterProxyModel):
    """Filter a QFileSystemModel using an eager scan of the outputs tree."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.accepted_paths: set[Path] | None = None

    def set_accepted_paths(self, paths: set[Path] | None) -> None:
        self.accepted_paths = paths
        self.invalidateFilter()

    def filterAcceptsRow(self, source_row: int, source_parent: QModelIndex) -> bool:
        index = self.sourceModel().index(source_row, 0, source_parent)
        file_path = Path(self.sourceModel().filePath(index))
        if not is_output_path(file_path) and not is_output_parent_path(file_path):
            return False

        if is_output_parent_path(file_path):
            return True
        if self.accepted_paths is None:
            return True
        return file_path.resolve(strict=False) in self.accepted_paths


class FileFinderWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("FileFinder V2")
        self.resize(1180, 760)

        self.game_root: Path | None = None
        self.mod_folder: Path | None = None
        self.last_cut_paths: list[Path] = []
        self.pre_filter_current_path: Path | None = None

        OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
        self._build_ui()
        self._connect_signals()
        self._apply_theme()
        self._resolve_game_root()
        self._setup_file_model()

    def _build_ui(self) -> None:
        central = QWidget()
        root = QVBoxLayout(central)
        root.setContentsMargins(20, 18, 20, 18)
        root.setSpacing(14)
        self.setCentralWidget(central)

        header = QHBoxLayout()
        title = QLabel("FileFinder V2")
        title.setObjectName("AppTitle")
        self.game_root_label = QLabel("Game root: not selected")
        self.game_root_label.setObjectName("MutedLabel")
        self.change_game_root_button = QPushButton("Change")
        self.change_game_root_button.setObjectName("SubtleButton")
        game_root_scope = QFrame()
        game_root_scope.setObjectName("HeaderScope")
        game_root_scope_layout = QHBoxLayout(game_root_scope)
        game_root_scope_layout.setContentsMargins(12, 6, 6, 6)
        game_root_scope_layout.setSpacing(10)
        game_root_scope_layout.addWidget(self.game_root_label, 1)
        game_root_scope_layout.addWidget(self.change_game_root_button)
        header.addWidget(title, 8)
        header.addWidget(game_root_scope, 3)
        root.addLayout(header)

        splitter = QSplitter(Qt.Horizontal)
        splitter.setChildrenCollapsible(False)
        root.addWidget(splitter, 1)

        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 10, 0)
        left_layout.setSpacing(14)
        splitter.addWidget(left)

        path_card, path_layout = self._card("Path Addition")

        input_row = QHBoxLayout()
        self.path_input = QLineEdit()
        self.path_input.setPlaceholderText("Paste asset path with archive prefix, then press Enter")
        self.add_button = QPushButton("Add")
        self.add_button.setObjectName("PrimaryButton")
        self.search_button = QPushButton("Search")
        self.search_button.setObjectName("SearchButton")
        input_row.addWidget(self.path_input, 1)
        input_row.addWidget(self.add_button)
        input_row.addWidget(self.search_button)
        path_layout.addLayout(input_row)

        self.path_table = QTableWidget(0, 2)
        self.path_table.setHorizontalHeaderLabels(["Archive Name", "File Path"])
        self.path_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.path_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.path_table.verticalHeader().setVisible(False)
        self.path_table.setAlternatingRowColors(True)
        self.path_table.setSelectionBehavior(QTableWidget.SelectRows)
        self.path_table.setEditTriggers(QTableWidget.NoEditTriggers)
        path_layout.addWidget(self.path_table, 1)
        left_layout.addWidget(path_card, 2)

        options_card, options_body = self._card("Options")
        options_layout = QGridLayout()
        options_body.addLayout(options_layout)
        options_layout.setColumnStretch(1, 1)
        self.mod_folder_checkbox = QCheckBox("Send into selected folder")
        self.mod_folder_label = QLabel("No folder selected")
        self.mod_folder_label.setObjectName("MutedLabel")
        self.browse_mod_folder_button = QPushButton("Browse")
        self.browse_mod_folder_button.setObjectName("SubtleButton")
        options_layout.addWidget(self.mod_folder_checkbox, 0, 0, 1, 2)
        options_layout.addWidget(self.mod_folder_label, 1, 0, 1, 2)
        options_layout.addWidget(self.browse_mod_folder_button, 1, 2)
        left_layout.addWidget(options_card)

        state_frame = QFrame()
        state_frame.setObjectName("StateFrame")
        state_layout = QHBoxLayout(state_frame)
        state_layout.setContentsMargins(10, 8, 10, 8)
        state_layout.setSpacing(10)
        self.status_label = QLabel("Ready")
        self.status_label.setObjectName("StatusLabel")
        self.clear_output_button = QPushButton("Clear Output Folder")
        self.clear_output_button.setObjectName("DangerButton")
        state_layout.addWidget(self.status_label, 1)
        state_layout.addWidget(self.clear_output_button, 0, Qt.AlignRight)
        left_layout.addWidget(state_frame)

        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(10, 0, 0, 0)
        right_layout.setSpacing(14)
        splitter.addWidget(right)

        output_card, output_layout = self._card("File Output")
        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("Search file name in outputs")
        output_layout.addWidget(self.search_input)

        self.file_tree = QTreeView()
        self.file_tree.setSelectionMode(QTreeView.ExtendedSelection)
        self.file_tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self.file_tree.setSortingEnabled(True)
        self.file_tree.setAlternatingRowColors(True)
        output_layout.addWidget(self.file_tree, 1)
        right_layout.addWidget(output_card, 1)

        splitter.setSizes([470, 710])

    def _card(self, title: str) -> tuple[QFrame, QVBoxLayout]:
        frame = QFrame()
        frame.setObjectName("Card")
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(16, 14, 16, 16)
        layout.setSpacing(12)
        label = QLabel(title)
        label.setObjectName("SectionTitle")
        layout.addWidget(label)
        body = QVBoxLayout()
        body.setSpacing(12)
        layout.addLayout(body, 1)
        return frame, body

    def _connect_signals(self) -> None:
        self.add_button.clicked.connect(self.add_path)
        self.path_input.returnPressed.connect(self.add_path)
        self.search_button.clicked.connect(self.search_paths)
        self.mod_folder_checkbox.toggled.connect(self.on_mod_folder_toggled)
        self.browse_mod_folder_button.clicked.connect(self.choose_mod_folder)
        self.change_game_root_button.clicked.connect(self.change_game_root)
        self.clear_output_button.clicked.connect(self.clear_output_folder)
        self.search_input.textChanged.connect(self.apply_file_filter)
        self.file_tree.customContextMenuRequested.connect(self.open_context_menu)

        QShortcut(QKeySequence.Copy, self.file_tree, activated=self.copy_selected_files)
        QShortcut(QKeySequence.Cut, self.file_tree, activated=self.cut_selected_files)
        QShortcut(QKeySequence.Delete, self.file_tree, activated=self.delete_selected_files)

    def _setup_file_model(self) -> None:
        self.file_model = QFileSystemModel(self)
        self.file_model.setRootPath(str(OUTPUT_ROOT))
        self.file_model.setFilter(QDir.AllEntries | QDir.NoDotAndDotDot)
        try:
            self.file_model.setOption(QFileSystemModel.Option.DontResolveSymlinks, True)
        except AttributeError:
            pass

        self.proxy_model = FileNameFilterProxy(self)
        self.proxy_model.setSourceModel(self.file_model)

        self.file_tree.setModel(self.proxy_model)
        root_index = self.proxy_model.mapFromSource(self.file_model.index(str(OUTPUT_ROOT)))
        self.file_tree.setRootIndex(root_index)
        self.file_tree.sortByColumn(0, Qt.AscendingOrder)
        self.configure_file_tree_columns()

    def configure_file_tree_columns(self) -> None:
        header = self.file_tree.header()
        header.setStretchLastSection(False)
        header.setSectionResizeMode(0, QHeaderView.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.Fixed)
        header.setSectionResizeMode(3, QHeaderView.ResizeToContents)

        base_type_width = 44
        self.file_tree.resizeColumnToContents(3)
        date_width = header.sectionSize(3)
        reclaimed_width = date_width // 2
        type_bonus = reclaimed_width // 4
        type_width = base_type_width + type_bonus
        shortened_date_width = date_width - reclaimed_width
        type_extension_ratio = 1.30
        date_extension_ratio = 1.56

        header.resizeSection(2, round(type_width * type_extension_ratio))
        header.setSectionResizeMode(3, QHeaderView.Fixed)
        header.resizeSection(3, round(shortened_date_width * date_extension_ratio))

    def _resolve_game_root(self) -> None:
        memory = load_memory()
        stored = memory.get("game_root", "").strip()
        if stored:
            self.set_game_root(Path(stored))
            return

        default_executable = find_default_game_executable()
        if default_executable is not None:
            result = QMessageBox.question(
                self,
                "Game Executable",
                f"Found game executable:\n\n{default_executable}\n\nUse this path?",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.Yes,
            )
            if result == QMessageBox.Yes:
                self.set_game_root(default_executable.parent, persist=True)
                return

        self.change_game_root()

    def set_game_root(self, game_root: Path, *, persist: bool = False) -> None:
        self.game_root = game_root
        self.game_root_label.setText(f"Game root: {game_root}")
        if persist:
            save_game_root(game_root)

    def change_game_root(self) -> None:
        selected, _filter = QFileDialog.getOpenFileName(
            self,
            "Game Executable",
            "",
            "dwrg.exe (dwrg.exe)",
        )
        if not selected:
            return
        executable_path = Path(selected)
        if executable_path.name.lower() != "dwrg.exe":
            QMessageBox.warning(self, "Game Executable", "Selected executable must be dwrg.exe")
            return
        game_root = executable_path.parent
        self.set_game_root(game_root, persist=True)

    def on_mod_folder_toggled(self, checked: bool) -> None:
        if checked and self.mod_folder is None:
            if not self.choose_mod_folder():
                self.mod_folder_checkbox.blockSignals(True)
                self.mod_folder_checkbox.setChecked(False)
                self.mod_folder_checkbox.blockSignals(False)
        self._update_mod_folder_label()

    def choose_mod_folder(self) -> bool:
        selected = QFileDialog.getExistingDirectory(self, "Select Folder")
        if not selected:
            return False
        self.mod_folder = Path(selected)
        self.mod_folder_checkbox.setChecked(True)
        self._update_mod_folder_label()
        return True

    def _update_mod_folder_label(self) -> None:
        if self.mod_folder_checkbox.isChecked() and self.mod_folder is not None:
            self.mod_folder_label.setText(str(self.mod_folder))
        else:
            self.mod_folder_label.setText("No folder selected")

    def add_path(self) -> None:
        raw_path = self.path_input.text().strip()
        if not raw_path:
            return
        if self.game_root is None:
            QMessageBox.warning(self, "Game Root", "Select a game executable first.")
            return

        try:
            archives = discover_archives(self.game_root)
            parsed = parse_asset_path(raw_path, archives)
            if self.has_queued_path(raw_path):
                self.status_label.setText("Path is already in the search list")
                return
            self.add_queue_row(raw_path, parsed.archive.prefix, parsed.normalized_path)
            self.path_input.clear()
            self.status_label.setText(f"Queued {self.path_table.rowCount()} path(s)")
        except Exception as exc:
            QMessageBox.critical(self, "Path Normalization Failed", str(exc))
            self.status_label.setText("Path addition failed")

    def has_queued_path(self, raw_path: str) -> bool:
        for row in range(self.path_table.rowCount()):
            item = self.path_table.item(row, 0)
            if item is not None and item.data(Qt.UserRole) == raw_path:
                return True
        return False

    def add_queue_row(self, raw_path: str, archive_name: str, normalized_path: str) -> None:
        row = self.path_table.rowCount()
        self.path_table.insertRow(row)
        archive = QTableWidgetItem(archive_name)
        archive.setData(Qt.UserRole, raw_path)
        archive.setToolTip(raw_path)
        path = QTableWidgetItem(normalized_path)
        path.setData(Qt.UserRole, raw_path)
        path.setToolTip("Pending search")
        self.path_table.setItem(row, 0, archive)
        self.path_table.setItem(row, 1, path)

    def queued_paths(self) -> list[str]:
        paths: list[str] = []
        for row in range(self.path_table.rowCount()):
            item = self.path_table.item(row, 0)
            if item is None:
                continue
            raw_path = item.data(Qt.UserRole)
            if isinstance(raw_path, str) and raw_path:
                paths.append(raw_path)
        return paths

    def search_paths(self) -> None:
        raw_paths = self.queued_paths()
        if not raw_paths:
            QMessageBox.information(self, "Search", "Add at least one path before searching.")
            return
        if self.game_root is None:
            QMessageBox.warning(self, "Game Root", "Select a game executable first.")
            return

        self.set_busy(True)
        try:
            report = extract_assets(
                self.game_root,
                raw_paths,
                output_root=OUTPUT_ROOT,
                decode=True,
            )
            self.update_queue_tooltips(report)
            copy_result = self.copy_to_mod_folder(report)
            self.refresh_output_root()
            status = self.report_status(report)
            if copy_result is not None:
                status += (
                    f"; copied {copy_result.copied} to selected folder"
                    f" ({copy_result.overwritten} overwritten, {copy_result.renamed} renamed)"
                )
            self.status_label.setText(status)
        except Exception as exc:
            QMessageBox.critical(self, "Search Failed", str(exc))
            self.status_label.setText("Search failed")
        finally:
            self.set_busy(False)

    def update_queue_tooltips(self, report: ExtractionReport) -> None:
        lookup_by_raw = {item.request.raw_path: item for item in report.lookups}
        missing_by_raw = {item.request.raw_path for item in report.missing}
        for row in range(self.path_table.rowCount()):
            raw_path = self.path_table.item(row, 0).data(Qt.UserRole)
            path_item = self.path_table.item(row, 1)
            lookup = lookup_by_raw.get(raw_path)
            if lookup is None:
                continue
            status = "Missing" if raw_path in missing_by_raw else "Extracted"
            path_item.setToolTip(f"{status}\nHash128: {lookup.lookup.hash128_hex}")

    def copy_to_mod_folder(self, report: ExtractionReport) -> ModCopyResult | None:
        if not self.mod_folder_checkbox.isChecked() or self.mod_folder is None:
            return None

        return copy_report_to_mod_folder(
            report,
            output_root=OUTPUT_ROOT,
            mod_folder=self.mod_folder,
            resolve_conflict=self.resolve_mod_copy_conflict,
        )

    def resolve_mod_copy_conflict(self, target: Path, renamed_target: Path) -> str:
        dialog = QMessageBox(self)
        dialog.setWindowTitle("Name Conflict")
        dialog.setIcon(QMessageBox.Warning)
        dialog.setText(f"File already exists:\n{target}")
        dialog.setInformativeText(f"Rename option:\n{renamed_target.name}")
        overwrite_button = dialog.addButton("Overwrite", QMessageBox.AcceptRole)
        rename_button = dialog.addButton("Rename (add prefix)", QMessageBox.ActionRole)
        dialog.setDefaultButton(rename_button)
        dialog.exec()
        if dialog.clickedButton() == overwrite_button:
            return "overwrite"
        return "rename"

    @staticmethod
    def report_status(report: ExtractionReport) -> str:
        if report.ok:
            return f"Extracted {len(report.written)} file(s)"
        return f"Extracted {len(report.written)} file(s), missing {len(report.missing)} file(s)"

    def set_busy(self, busy: bool) -> None:
        self.add_button.setEnabled(not busy)
        self.search_button.setEnabled(not busy)
        self.path_input.setEnabled(not busy)
        if busy:
            QApplication.setOverrideCursor(QCursor(Qt.WaitCursor))
            return
        QApplication.restoreOverrideCursor()
        if not busy:
            self.path_input.setFocus()

    def refresh_output_root(self) -> None:
        self.file_model.setRootPath(str(OUTPUT_ROOT))
        root_index = self.proxy_model.mapFromSource(self.file_model.index(str(OUTPUT_ROOT)))
        self.file_tree.setRootIndex(root_index)

    def apply_file_filter(self, text: str) -> None:
        query = text.strip().lower()
        if not query:
            self.proxy_model.set_accepted_paths(None)
            self.refresh_output_root()
            self.restore_current_output_directory()
            self.pre_filter_current_path = None
            self.status_label.setText("Ready")
            return

        self.pre_filter_current_path = self.current_output_directory()

        accepted_paths = self.find_output_matches(query)
        if not accepted_paths:
            self.proxy_model.set_accepted_paths(None)
            self.refresh_output_root()
            self.restore_current_output_directory()
            self.status_label.setText(f"No output file matched '{text.strip()}'")
            return

        self.proxy_model.set_accepted_paths(accepted_paths)
        self.refresh_output_root()
        self.file_tree.expandAll()
        self.status_label.setText(f"Found {self.count_leaf_matches(query)} matching output item(s)")

    def current_output_directory(self) -> Path:
        proxy_index = self.file_tree.currentIndex()
        if not proxy_index.isValid():
            return OUTPUT_ROOT

        source_index = self.proxy_model.mapToSource(proxy_index)
        path = Path(self.file_model.filePath(source_index))
        if not is_output_path(path):
            return OUTPUT_ROOT
        if path.is_file():
            return path.parent
        return path

    def restore_current_output_directory(self) -> None:
        if self.pre_filter_current_path is None:
            return
        source_index = self.file_model.index(str(self.pre_filter_current_path))
        proxy_index = self.proxy_model.mapFromSource(source_index)
        if proxy_index.isValid():
            self.file_tree.setCurrentIndex(proxy_index)
            self.file_tree.scrollTo(proxy_index)

    def find_output_matches(self, query: str) -> set[Path]:
        accepted_paths: set[Path] = {OUTPUT_ROOT_RESOLVED}
        found = False

        for path in OUTPUT_ROOT.rglob("*"):
            if not is_output_path(path):
                continue
            if query not in path.name.lower():
                continue

            found = True
            resolved = path.resolve(strict=False)
            accepted_paths.add(resolved)
            for parent in resolved.parents:
                if parent == OUTPUT_ROOT_RESOLVED:
                    accepted_paths.add(parent)
                    break
                if is_output_path(parent):
                    accepted_paths.add(parent.resolve(strict=False))

        return accepted_paths if found else set()

    def count_leaf_matches(self, query: str) -> int:
        return sum(
            1
            for path in OUTPUT_ROOT.rglob("*")
            if is_output_path(path) and query in path.name.lower()
        )

    def selected_paths(self) -> list[Path]:
        paths: list[Path] = []
        seen: set[str] = set()
        for proxy_index in self.file_tree.selectionModel().selectedRows(0):
            source_index = self.proxy_model.mapToSource(proxy_index)
            path = Path(self.file_model.filePath(source_index))
            if not is_output_path(path):
                continue
            key = str(path)
            if key not in seen:
                seen.add(key)
                paths.append(path)
        return paths

    def copy_selected_files(self) -> None:
        paths = self.selected_paths()
        if not paths:
            return
        mime = QMimeData()
        mime.setUrls([QUrl.fromLocalFile(str(path)) for path in paths])
        mime.setText("\n".join(str(path) for path in paths))
        QApplication.clipboard().setMimeData(mime)
        self.last_cut_paths = []
        self.status_label.setText(f"Copied {len(paths)} item(s) to clipboard")

    def cut_selected_files(self) -> None:
        paths = self.selected_paths()
        if not paths:
            return
        mime = QMimeData()
        mime.setUrls([QUrl.fromLocalFile(str(path)) for path in paths])
        mime.setText("\n".join(str(path) for path in paths))
        QApplication.clipboard().setMimeData(mime)
        self.last_cut_paths = paths
        self.status_label.setText(f"Marked {len(paths)} item(s) for cut")

    def delete_selected_files(self) -> None:
        paths = self.selected_paths()
        if not paths:
            return
        result = QMessageBox.question(
            self,
            "Delete",
            f"Delete {len(paths)} selected item(s)?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if result != QMessageBox.Yes:
            return

        errors: list[str] = []
        for path in sorted(paths, key=lambda value: len(value.parts), reverse=True):
            if not is_output_path(path):
                errors.append(f"{path}: path is outside outputs")
                continue
            try:
                if path.is_dir():
                    shutil.rmtree(path)
                elif path.exists():
                    path.unlink()
            except Exception as exc:
                errors.append(f"{path}: {exc}")

        if errors:
            QMessageBox.warning(self, "Delete", "\n".join(errors[:8]))
        self.status_label.setText(f"Deleted {len(paths) - len(errors)} item(s)")

    def clear_output_folder(self) -> None:
        result = QMessageBox.question(
            self,
            "Clear Output Folder",
            f"Delete all files and folders inside outputs?\n\n{OUTPUT_ROOT}",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if result != QMessageBox.Yes:
            return

        errors: list[str] = []
        for path in OUTPUT_ROOT.iterdir():
            if not is_output_path(path):
                errors.append(f"{path}: path is outside outputs")
                continue
            try:
                if path.is_dir():
                    shutil.rmtree(path)
                else:
                    path.unlink()
            except Exception as exc:
                errors.append(f"{path}: {exc}")

        self.refresh_output_root()
        if errors:
            QMessageBox.warning(self, "Clear Output Folder", "\n".join(errors[:8]))
        self.status_label.setText("Output folder cleared" if not errors else "Output folder cleared with errors")

    def open_context_menu(self, position) -> None:
        menu = QMenu(self)
        copy_action = QAction("Copy", self)
        cut_action = QAction("Cut", self)
        delete_action = QAction("Delete", self)
        open_location_action = QAction("Open file location in File Explorer", self)

        copy_action.triggered.connect(self.copy_selected_files)
        cut_action.triggered.connect(self.cut_selected_files)
        delete_action.triggered.connect(self.delete_selected_files)
        open_location_action.triggered.connect(self.open_selected_location)

        has_selection = bool(self.selected_paths())
        for action in (copy_action, cut_action, delete_action, open_location_action):
            action.setEnabled(has_selection)

        menu.addAction(copy_action)
        menu.addAction(cut_action)
        menu.addSeparator()
        menu.addAction(delete_action)
        menu.addSeparator()
        menu.addAction(open_location_action)
        menu.exec(self.file_tree.viewport().mapToGlobal(position))

    def open_selected_location(self) -> None:
        paths = self.selected_paths()
        if not paths:
            return
        path = paths[0]
        if not is_output_path(path):
            QMessageBox.warning(self, "Open Location", "Selected path is outside outputs.")
            return
        if sys.platform.startswith("win"):
            if path.is_file():
                subprocess.Popen(["explorer", "/select,", str(path)])
            else:
                subprocess.Popen(["explorer", str(path)])
        else:
            subprocess.Popen(["xdg-open", str(path.parent if path.is_file() else path)])

    def _apply_theme(self) -> None:
        self.setStyleSheet(
            """
            QMainWindow, QWidget {
                background: #0f131a;
                color: #e6eaf2;
                font-size: 10pt;
            }
            #AppTitle {
                font-size: 22pt;
                font-weight: 700;
                color: #f6f8fb;
            }
            #SectionTitle {
                font-size: 13pt;
                font-weight: 650;
                color: #f6f8fb;
                background: #161b24;
                margin-bottom: 6px;
            }
            #Card, #CardContent {
                background: #161b24;
                border: 1px solid #283140;
                border-radius: 8px;
            }
            #CardContent {
                padding: 0;
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
            }
            QPushButton:hover {
                background: #2c3647;
            }
            QPushButton:pressed {
                background: #1d2532;
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
            #SearchButton {
                background: #238636;
                border-color: #3fb950;
                color: white;
                font-weight: 650;
                min-width: 92px;
            }
            #SearchButton:hover {
                background: #2ea043;
            }
            #SubtleButton {
                padding: 7px 12px;
            }
            #DangerButton {
                background: #8b1e2d;
                border-color: #cf4458;
                color: white;
                font-weight: 650;
                padding: 7px 12px;
            }
            #DangerButton:hover {
                background: #a5283a;
            }
            #HeaderScope {
                background: #151b25;
                border: 1px solid #283140;
                border-radius: 8px;
            }
            #StateFrame {
                background: #151b25;
                border: 1px solid #283140;
                border-radius: 8px;
            }
            QCheckBox {
                spacing: 10px;
                font-weight: 600;
                background: #161b24;
            }
            QCheckBox::indicator {
                width: 18px;
                height: 18px;
                border-radius: 5px;
                border: 1px solid #536075;
                background: #0d1118;
            }
            QCheckBox::indicator:checked {
                background: #2f81f7;
                border-color: #80b5ff;
            }
            QTableWidget, QTreeView {
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
            QTreeView::item, QTableWidget::item {
                padding: 6px;
            }
            QSplitter::handle {
                background: #202837;
                margin: 2px;
            }
            QMenu {
                background: #151b25;
                border: 1px solid #344055;
                padding: 6px;
            }
            QMenu::item {
                padding: 8px 24px;
                border-radius: 5px;
            }
            QMenu::item:selected {
                background: #244f87;
            }
            #MutedLabel {
                color: #98a5b8;
                background: #151b25;
            }
            #StatusLabel {
                color: #9ecbff;
                background: #151b25;
                padding: 6px 2px;
            }
            """
        )

    def closeEvent(self, event: QCloseEvent) -> None:
        QApplication.restoreOverrideCursor()
        super().closeEvent(event)


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("FileFinder V2")
    window = FileFinderWindow()
    window.showMaximized()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
