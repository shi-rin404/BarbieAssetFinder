#!/usr/bin/env python3
"""PySide6 GUI for FileFinderV2."""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

from PySide6.QtCore import QDir, QMimeData, QModelIndex, QSortFilterProxyModel, Qt, QUrl, Signal
from PySide6.QtGui import QAction, QCloseEvent, QCursor, QDesktopServices, QDrag, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QFileDialog,
    QFileSystemModel,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
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

from filefinder.archive.idx_wpk import ArchiveIndexCache
from filefinder.core.extract import ExtractionReport, extract_assets
from filefinder.core.memory import (
    find_default_game_executable,
    load_memory,
    save_game_root,
)
from filefinder.core.mod_copy import ModCopyResult, copy_report_to_mod_folder
from filefinder.core.paths import discover_archives, parse_asset_path
from filefinder.core.tracking import extract_assets_with_tracking


APP_DIR = Path(__file__).resolve().parent
OUTPUT_ROOT = APP_DIR / "outputs"
OUTPUT_ROOT_RESOLVED = OUTPUT_ROOT.resolve()


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


def merge_extraction_reports(reports: list[ExtractionReport]) -> ExtractionReport:
    lookups = []
    written = []
    missing = []
    seen_lookups: set[tuple[str, str, str]] = set()
    seen_written: set[tuple[str, Path]] = set()
    seen_missing: set[tuple[str, str]] = set()

    for report in reports:
        for item in report.lookups:
            key = (item.request.raw_path, item.request.archive.prefix, item.request.normalized_path)
            if key in seen_lookups:
                continue
            seen_lookups.add(key)
            lookups.append(item)
        for item in report.written:
            key = (item.request.raw_path, item.output_path)
            if key in seen_written:
                continue
            seen_written.add(key)
            written.append(item)
        for item in report.missing:
            key = (item.request.raw_path, item.hash128_hex)
            if key in seen_missing:
                continue
            seen_missing.add(key)
            missing.append(item)

    return ExtractionReport(
        lookups=tuple(lookups),
        written=tuple(written),
        missing=tuple(missing),
    )


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


class OutputTreeView(QTreeView):
    """Tree view that exports selected output files as native file-url drags."""

    def startDrag(self, supported_actions: Qt.DropActions) -> None:
        paths: list[Path] = []
        model = self.model()
        if model is None:
            return

        for proxy_index in self.selectionModel().selectedRows(0):
            source_index = model.mapToSource(proxy_index)
            source_model = model.sourceModel()
            path = Path(source_model.filePath(source_index))
            if is_output_path(path):
                paths.append(path)

        if not paths:
            return

        mime = QMimeData()
        mime.setUrls([QUrl.fromLocalFile(str(path)) for path in paths])
        mime.setText("\n".join(str(path) for path in paths))

        drag = QDrag(self)
        drag.setMimeData(mime)
        drag.exec(Qt.MoveAction | Qt.CopyAction, Qt.MoveAction)


class PersistentCheckMenu(QMenu):
    """Keep checkable menu selections open until the user clicks outside."""

    def mouseReleaseEvent(self, event) -> None:
        action = self.activeAction()
        if action is None:
            super().mouseReleaseEvent(event)
            return
        if action.isCheckable():
            action.trigger()
            event.accept()
            return
        if action.menu() is not None:
            event.accept()
            return
        super().mouseReleaseEvent(event)


class CheckableComboBox(QPushButton):
    """Combo-like multi-select button backed by a checkable menu."""

    changed = Signal()

    def __init__(
        self,
        placeholder: str,
        items: list[str],
        *,
        nested_items: dict[str, list[str]] | None = None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.placeholder = placeholder
        self.actions_by_text: dict[str, QAction] = {}
        self.nested_actions: dict[str, dict[str, QAction]] = {}
        self.dropdown_menu = PersistentCheckMenu(self)
        self.ignore_next_popup = False
        self.setObjectName("ComboButton")
        self.clicked.connect(self.toggle_dropdown)
        self.dropdown_menu.aboutToHide.connect(self.on_dropdown_about_to_hide)

        nested_items = nested_items or {}
        for text in items:
            if text in nested_items:
                submenu = PersistentCheckMenu(text, self.dropdown_menu)
                submenu.setObjectName("ComboMenu")
                submenu.aboutToHide.connect(self.on_dropdown_about_to_hide)
                self.nested_actions[text] = {}
                for child_text in nested_items[text]:
                    child_action = QAction(child_text, submenu)
                    child_action.setCheckable(True)
                    child_action.toggled.connect(self.on_action_toggled)
                    submenu.addAction(child_action)
                    self.nested_actions[text][child_text] = child_action
                self.dropdown_menu.addMenu(submenu)
                continue

            action = QAction(text, self.dropdown_menu)
            action.setCheckable(True)
            action.toggled.connect(self.on_action_toggled)
            self.dropdown_menu.addAction(action)
            self.actions_by_text[text] = action
        self.update_display_text()

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.LeftButton and self.isEnabled():
            self.toggle_dropdown()
            event.accept()
            return
        super().mousePressEvent(event)

    def toggle_dropdown(self) -> None:
        if self.ignore_next_popup:
            self.ignore_next_popup = False
            return
        if self.dropdown_menu.isVisible():
            self.dropdown_menu.hide()
            return
        self.show_dropdown()

    def show_dropdown(self) -> None:
        self.dropdown_menu.popup(self.mapToGlobal(self.rect().bottomLeft()))

    def on_dropdown_about_to_hide(self) -> None:
        if self.rect().contains(self.mapFromGlobal(QCursor.pos())):
            self.ignore_next_popup = True

    def checked_items(self) -> set[str]:
        checked = {text for text, action in self.actions_by_text.items() if action.isChecked()}
        for text, actions in self.nested_actions.items():
            if any(action.isChecked() for action in actions.values()):
                checked.add(text)
        return checked

    def set_checked(self, text: str, checked: bool) -> None:
        action = self.actions_by_text.get(text)
        if action is None:
            return
        action.setChecked(checked)

    def is_checked(self, text: str) -> bool:
        action = self.actions_by_text.get(text)
        return bool(action and action.isChecked())

    def nested_checked_items(self, text: str) -> set[str]:
        return {
            child_text
            for child_text, action in self.nested_actions.get(text, {}).items()
            if action.isChecked()
        }

    def on_action_toggled(self) -> None:
        self.sync_texture_grab_all()
        self.update_display_text()
        self.changed.emit()

    def update_display_text(self) -> None:
        selected = sorted(self.checked_items())
        self.setText(", ".join(selected) if selected else self.placeholder)

    def sync_texture_grab_all(self) -> None:
        texture_actions = self.nested_actions.get("Texture", {})
        grab_all = texture_actions.get("Grab All")
        if grab_all is None:
            return

        muted = grab_all.isChecked()
        for text in ("Diffuse", "Normal", "Metal"):
            action = texture_actions.get(text)
            if action is None:
                continue
            previous_state = action.blockSignals(True)
            if muted:
                action.setChecked(False)
            action.setEnabled(not muted)
            action.blockSignals(previous_state)


class FileFinderWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("FileFinder V2")
        self.resize(1180, 760)

        self.game_root: Path | None = None
        self.mod_folder: Path | None = None
        self.last_cut_paths: list[Path] = []
        self.pre_filter_current_path: Path | None = None
        self.archive_index_cache = ArchiveIndexCache()

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
        self.path_input.setPlaceholderText("Paste asset path with archive prefix")
        self.add_button = QPushButton("Add")
        self.add_button.setObjectName("PrimaryButton")
        self.add_button.setToolTip("Shortcut: Enter")
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
        self.path_table.setContextMenuPolicy(Qt.CustomContextMenu)
        path_layout.addWidget(self.path_table, 1)
        left_layout.addWidget(path_card, 2)

        options_card, options_body = self._card("Options")
        options_layout = QGridLayout()
        options_body.addLayout(options_layout)
        options_layout.setColumnStretch(1, 1)
        self.mod_folder_checkbox = QCheckBox("Send into selected folder")
        self.mod_folder_label = QLabel("No folder selected")
        self.mod_folder_label.setObjectName("OptionPathLabel")
        self.mod_folder_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.browse_mod_folder_button = QPushButton("Browse")
        self.browse_mod_folder_button.setObjectName("SubtleButton")
        self.auto_decode_checkbox = QCheckBox("Auto Decode NX-XML")
        self.auto_decode_checkbox.setChecked(True)
        self.file_tracking_combo = CheckableComboBox(
            "File Tracking",
            ["Mesh", "Texture", "GIM", "MTL", "MTG", "STB"],
            nested_items={"Texture": ["Diffuse", "Normal", "Metal", "Grab All"]},
        )
        tracking_options_row = QHBoxLayout()
        tracking_options_row.addWidget(self.auto_decode_checkbox, 0, Qt.AlignLeft)
        tracking_options_row.addStretch(1)
        tracking_options_row.addWidget(self.file_tracking_combo, 0, Qt.AlignRight)
        options_layout.addWidget(self.mod_folder_checkbox, 0, 0)
        options_layout.addWidget(self.mod_folder_label, 0, 1, Qt.AlignRight)
        options_layout.addWidget(self.browse_mod_folder_button, 0, 2, Qt.AlignRight)
        options_layout.addLayout(tracking_options_row, 1, 0, 1, 3)
        self.mod_folder_label.setVisible(False)
        self.browse_mod_folder_button.setVisible(False)
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

        self.file_tree = OutputTreeView()
        self.file_tree.setSelectionMode(QTreeView.ExtendedSelection)
        self.file_tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self.file_tree.setSortingEnabled(True)
        self.file_tree.setAlternatingRowColors(True)
        self.file_tree.setDragEnabled(True)
        self.file_tree.setDragDropMode(QAbstractItemView.DragOnly)
        self.file_tree.setDefaultDropAction(Qt.MoveAction)
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
        self.path_table.customContextMenuRequested.connect(self.open_queue_context_menu)
        self.file_tree.customContextMenuRequested.connect(self.open_context_menu)
        self.file_tree.doubleClicked.connect(self.open_file_from_index)

        add_return_shortcut = QShortcut(QKeySequence(Qt.Key_Return), self.path_input, activated=self.add_path)
        add_return_shortcut.setContext(Qt.WidgetShortcut)
        add_enter_shortcut = QShortcut(QKeySequence(Qt.Key_Enter), self.path_input, activated=self.add_path)
        add_enter_shortcut.setContext(Qt.WidgetShortcut)
        QShortcut(QKeySequence.Delete, self.path_table, activated=self.delete_selected_queue_rows)
        QShortcut(QKeySequence(Qt.Key_Return), self.path_table, activated=self.edit_selected_queue_row)
        QShortcut(QKeySequence(Qt.Key_Enter), self.path_table, activated=self.edit_selected_queue_row)
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
            if ask_yes_no(
                self,
                "Game Executable",
                f"Found game executable:\n\n{default_executable}\n\nUse this path?",
                default_yes=True,
            ):
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
        enabled = self.mod_folder_checkbox.isChecked()
        self.mod_folder_label.setVisible(enabled)
        self.browse_mod_folder_button.setVisible(enabled)
        if enabled and self.mod_folder is not None:
            self.mod_folder_label.setText(str(self.mod_folder))
            self.mod_folder_label.setToolTip(str(self.mod_folder))
        else:
            self.mod_folder_label.setText("No folder selected")
            self.mod_folder_label.setToolTip("")

    def add_path(self) -> None:
        raw_path = self.path_input.text().strip()
        if not raw_path:
            return
        if self.queue_path(raw_path):
            self.path_input.clear()
            self.status_label.setText(f"Queued {self.path_table.rowCount()} path(s)")

    def queue_path(self, raw_path: str) -> bool:
        if self.game_root is None:
            QMessageBox.warning(self, "Game Root", "Select a game executable first.")
            return False

        try:
            archives = discover_archives(self.game_root)
            parsed = parse_asset_path(raw_path, archives)
            if self.has_queued_path(raw_path):
                self.status_label.setText("Path is already in the search list")
                return True
            self.add_queue_row(raw_path, parsed.archive.stem, parsed.normalized_path)
            return True
        except Exception as exc:
            QMessageBox.critical(self, "Path Normalization Failed", str(exc))
            self.status_label.setText("Path addition failed")
            return False

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

    def selected_queue_rows(self) -> list[int]:
        return sorted({index.row() for index in self.path_table.selectionModel().selectedRows()})

    def delete_selected_queue_rows(self) -> None:
        rows = self.selected_queue_rows()
        if not rows:
            return
        for row in sorted(rows, reverse=True):
            self.path_table.removeRow(row)
        self.status_label.setText(f"Queued {self.path_table.rowCount()} path(s)")

    def edit_selected_queue_row(self) -> None:
        rows = self.selected_queue_rows()
        if not rows:
            return
        self.edit_queue_row(rows[0])

    def edit_queue_row(self, row: int) -> None:
        raw_item = self.path_table.item(row, 0)
        if raw_item is None:
            return

        current_path = raw_item.data(Qt.UserRole)
        if not isinstance(current_path, str):
            current_path = ""

        new_path, accepted = QInputDialog.getText(
            self,
            "Edit Path",
            "Path:",
            QLineEdit.Normal,
            current_path,
        )
        if not accepted:
            return

        new_path = new_path.strip()
        if not new_path:
            return
        if self.game_root is None:
            QMessageBox.warning(self, "Game Root", "Select a game executable first.")
            return

        try:
            archives = discover_archives(self.game_root)
            parsed = parse_asset_path(new_path, archives)
            for existing_row in range(self.path_table.rowCount()):
                if existing_row == row:
                    continue
                item = self.path_table.item(existing_row, 0)
                if item is not None and item.data(Qt.UserRole) == new_path:
                    self.status_label.setText("Path is already in the search list")
                    return

            archive_item = self.path_table.item(row, 0)
            path_item = self.path_table.item(row, 1)
            archive_item.setText(parsed.archive.stem)
            archive_item.setData(Qt.UserRole, new_path)
            archive_item.setToolTip(new_path)
            path_item.setText(parsed.normalized_path)
            path_item.setData(Qt.UserRole, new_path)
            path_item.setToolTip("Pending search")
            self.status_label.setText("Queued path updated")
        except Exception as exc:
            QMessageBox.critical(self, "Path Edit Failed", str(exc))
            self.status_label.setText("Path edit failed")

    def open_queue_context_menu(self, position) -> None:
        row = self.path_table.rowAt(position.y())
        if row >= 0 and row not in self.selected_queue_rows():
            self.path_table.selectRow(row)

        menu = QMenu(self)
        edit_action = QAction("Edit Path", self)
        delete_action = QAction("Delete", self)
        edit_action.triggered.connect(self.edit_selected_queue_row)
        delete_action.triggered.connect(self.delete_selected_queue_rows)

        has_selection = bool(self.selected_queue_rows())
        edit_action.setEnabled(has_selection)
        delete_action.setEnabled(has_selection)

        menu.addAction(edit_action)
        menu.addSeparator()
        menu.addAction(delete_action)
        menu.exec(self.path_table.viewport().mapToGlobal(position))

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
        pending_path = self.path_input.text().strip()
        if pending_path:
            if not self.queue_path(pending_path):
                return
            self.path_input.clear()

        raw_paths = self.queued_paths()
        if not raw_paths:
            QMessageBox.information(self, "Search", "Add at least one path before searching.")
            return
        if self.game_root is None:
            QMessageBox.warning(self, "Game Root", "Select a game executable first.")
            return

        self.set_busy(True)
        try:
            file_tracking = self.file_tracking_combo.checked_items()
            texture_tracking = self.file_tracking_combo.nested_checked_items("Texture")
            auto_decode_nx_xml = self.auto_decode_checkbox.isChecked()
            report, skipped_errors, tracking_errors = self.extract_queued_paths(
                raw_paths,
                file_tracking=file_tracking,
                texture_tracking=texture_tracking,
                auto_decode_nx_xml=auto_decode_nx_xml,
            )
            self.update_queue_tooltips(report)
            copy_result = self.copy_to_mod_folder(report) if report.written else None
            if report.written:
                self.reveal_extracted_output_folders(report)
            status = self.report_status(report)
            if copy_result is not None:
                status += (
                    f"; copied {copy_result.copied} to selected folder"
                    f" ({copy_result.overwritten} overwritten, {copy_result.renamed} renamed)"
                )
            if skipped_errors:
                status += f"; skipped {len(skipped_errors)} file(s)"
            if tracking_errors:
                status += f"; {len(tracking_errors)} file tracking warning(s)"
            self.status_label.setText(status)
            if skipped_errors:
                QMessageBox.warning(
                    self,
                    "Search Skipped Files",
                    "\n\n".join(f"{raw_path}\n{error}" for raw_path, error in skipped_errors[:8]),
                )
            if tracking_errors:
                QMessageBox.warning(
                    self,
                    "File Tracking Warnings",
                    "\n\n".join(
                        f"{raw_path}\n{message}"
                        for raw_path, message in tracking_errors[:12]
                    ),
                )
            self.clear_queue()
        except Exception as exc:
            QMessageBox.critical(self, "Search Failed", str(exc))
            self.status_label.setText("Search failed")
        finally:
            self.set_busy(False)

    def extract_queued_paths(
        self,
        raw_paths: list[str],
        *,
        file_tracking: set[str],
        texture_tracking: set[str],
        auto_decode_nx_xml: bool,
    ) -> tuple[ExtractionReport, list[tuple[str, str]], list[tuple[str, str]]]:
        reports: list[ExtractionReport] = []
        skipped_errors: list[tuple[str, str]] = []
        tracking_errors: list[tuple[str, str]] = []
        for raw_path in raw_paths:
            try:
                if file_tracking:
                    def record_tracking_error(context: str, exc: Exception, current_path: str = raw_path) -> None:
                        tracking_errors.append(
                            (current_path, f"{context}: {type(exc).__name__}: {exc}")
                        )

                    reports.append(
                        extract_assets_with_tracking(
                            self.game_root,
                            [raw_path],
                            output_root=OUTPUT_ROOT,
                            file_types=file_tracking,
                            texture_types=texture_tracking,
                            auto_decode_nx_xml=auto_decode_nx_xml,
                            suppressed_error_callback=record_tracking_error,
                            index_cache=self.archive_index_cache,
                        )
                    )
                else:
                    reports.append(
                        extract_assets(
                            self.game_root,
                            [raw_path],
                            output_root=OUTPUT_ROOT,
                            auto_decode_nx_xml=auto_decode_nx_xml,
                            index_cache=self.archive_index_cache,
                        )
                    )
            except Exception as exc:
                skipped_errors.append((raw_path, str(exc)))
        return merge_extraction_reports(reports), skipped_errors, tracking_errors

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

    def clear_queue(self) -> None:
        self.path_table.setRowCount(0)

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
        self.mod_folder_checkbox.setEnabled(not busy)
        self.browse_mod_folder_button.setEnabled(not busy)
        self.auto_decode_checkbox.setEnabled(not busy)
        self.file_tracking_combo.setEnabled(not busy)
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

    def clear_output_filter_for_reveal(self) -> None:
        if self.search_input.text():
            self.search_input.blockSignals(True)
            self.search_input.clear()
            self.search_input.blockSignals(False)
        self.proxy_model.set_accepted_paths(None)
        self.pre_filter_current_path = None

    def reveal_extracted_output_folders(self, report: ExtractionReport) -> None:
        self.clear_output_filter_for_reveal()
        self.refresh_output_root()
        self.file_tree.collapseAll()

        folders = sorted(
            {item.output_path.parent for item in report.written if is_output_path(item.output_path.parent)},
            key=lambda path: len(path.parts),
        )
        first_folder_index = QModelIndex()

        for folder in folders:
            current = OUTPUT_ROOT
            for part in folder.resolve(strict=False).relative_to(OUTPUT_ROOT_RESOLVED).parts:
                current = current / part
                source_index = self.file_model.index(str(current))
                proxy_index = self.proxy_model.mapFromSource(source_index)
                if proxy_index.isValid():
                    self.file_tree.expand(proxy_index)
                    if not first_folder_index.isValid():
                        first_folder_index = proxy_index

        if first_folder_index.isValid():
            self.file_tree.setCurrentIndex(first_folder_index)
            self.file_tree.scrollTo(first_folder_index)

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

    def output_path_from_index(self, proxy_index: QModelIndex) -> Path | None:
        if not proxy_index.isValid():
            return None
        row_index = proxy_index.siblingAtColumn(0)
        source_index = self.proxy_model.mapToSource(row_index)
        path = Path(self.file_model.filePath(source_index))
        if not is_output_path(path):
            return None
        return path

    def open_file_from_index(self, proxy_index: QModelIndex) -> None:
        path = self.output_path_from_index(proxy_index)
        if path is None or not path.is_file():
            return
        self.open_file_path(path)

    def open_selected_file(self) -> None:
        file_path = next((path for path in self.selected_paths() if path.is_file()), None)
        if file_path is None:
            return
        self.open_file_path(file_path)

    def open_file_path(self, path: Path) -> None:
        if not is_output_path(path):
            QMessageBox.warning(self, "Open", "Selected path is outside outputs.")
            return
        if not path.is_file():
            return
        opened = QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))
        if not opened:
            QMessageBox.warning(self, "Open", f"Could not open file:\n{path}")

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
        if not ask_yes_no(
            self,
            "Delete",
            f"Delete {len(paths)} selected item(s)?",
            default_yes=False,
            icon=QMessageBox.Warning,
        ):
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
        if not ask_yes_no(
            self,
            "Clear Output Folder",
            f"Delete all files and folders inside outputs?\n\n{OUTPUT_ROOT}",
            default_yes=False,
            icon=QMessageBox.Warning,
        ):
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
        open_action = QAction("Open", self)
        copy_action = QAction("Copy", self)
        cut_action = QAction("Cut", self)
        delete_action = QAction("Delete", self)
        open_location_action = QAction("Open file location in File Explorer", self)

        open_action.triggered.connect(self.open_selected_file)
        copy_action.triggered.connect(self.copy_selected_files)
        cut_action.triggered.connect(self.cut_selected_files)
        delete_action.triggered.connect(self.delete_selected_files)
        open_location_action.triggered.connect(self.open_selected_location)

        selected_paths = self.selected_paths()
        has_selection = bool(selected_paths)
        open_action.setEnabled(any(path.is_file() for path in selected_paths))
        for action in (copy_action, cut_action, delete_action, open_location_action):
            action.setEnabled(has_selection)

        menu.addAction(open_action)
        menu.addSeparator()
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
            #ComboButton {
                background: #0d1118;
                border: 1px solid #2e394a;
                border-radius: 7px;
                padding: 9px 12px;
                min-height: 24px;
                text-align: left;
            }
            #ComboButton:hover {
                border-color: #4f8cff;
                background: #111722;
            }
            #ComboButton:disabled {
                color: #747d8d;
                background: #171c25;
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
            QMenu::item:disabled {
                color: #98a5b8;
                background: transparent;
            }
            #MutedLabel {
                color: #98a5b8;
                background: #151b25;
            }
            #OptionPathLabel {
                color: #98a5b8;
                background: #161b24;
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
