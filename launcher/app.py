#!/usr/bin/env python3
"""ForgeFast Launcher -- the application.

Screens and layer names come from the supplied Figma document, decoded from `Untitled.fig`: the
container is a zip, `canvas.fig` is `fig-kiwi` with a deflate-compressed schema followed by a
**zstd**-compressed body (not deflate, which is why a first attempt at inflating it failed).

Seven frames, built here as stacked pages:
    Home  ·  Add Profile  ·  Profile Details  ·  Launching  ·  Performance Stats  ·  Settings  ·  Error

Colours are in `theme.py` and were measured from the file, not chosen. Everything is wired to the
same `forgefast_launcher` core the CLI uses, so Play applies the real optimizations and Sign in runs
the real Microsoft device-code flow.
"""
from __future__ import annotations

import sys
import threading
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, Signal, QObject
from PySide6.QtGui import QFont, QPainter, QColor, QLinearGradient, QBrush
from PySide6.QtWidgets import (QApplication, QWidget, QLabel, QPushButton, QVBoxLayout, QHBoxLayout,
                               QLineEdit, QStackedWidget, QScrollArea, QFileDialog, QTextEdit,
                               QSlider, QProgressBar, QFrame, QGridLayout, QMessageBox)

sys.path.insert(0, str(Path(__file__).resolve().parent))
import forgefast_launcher as core
import theme


def label(text: str, object_name: str = "", size: int | None = None, bold: bool = False,
          color: str | None = None) -> QLabel:
    widget = QLabel(text)
    if object_name:
        widget.setObjectName(object_name)
    style = []
    if size:
        style.append(f"font-size:{size}px")
    if bold:
        style.append("font-weight:700")
    if color:
        style.append(f"color:{color}")
    if style:
        widget.setStyleSheet(";".join(style))
    return widget


class Bridge(QObject):
    """Worker threads cannot touch widgets, so launch output crosses back as signals."""
    line = Signal(str)
    finished = Signal(object, int, str)
    auth_step = Signal(str)
    auth_done = Signal(bool, str)


class BarChart(QWidget):
    """`bar-chart` / `dummy-chart-graphic`: launch time over past sessions.

    Hand-painted because the design's gradient bars are not expressible as a Qt stylesheet.
    """

    def __init__(self) -> None:
        super().__init__()
        self.values: list[float] = []
        self.setMinimumHeight(150)

    def set_values(self, values: list[float]) -> None:
        self.values = values[-12:]
        self.update()

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        if not self.values:
            painter.setPen(QColor(theme.TEXT_FAINT))
            painter.drawText(self.rect(), Qt.AlignCenter, "No launches recorded yet")
            return
        top, bottom, left = 10, self.height() - 22, 6
        peak = max(self.values) or 1.0
        slot = (self.width() - left * 2) / max(1, len(self.values))
        width = min(38, slot * 0.62)
        best = min(self.values)
        for index, value in enumerate(self.values):
            height = max(3.0, (value / peak) * (bottom - top))
            x = left + index * slot + (slot - width) / 2
            y = bottom - height
            gradient = QLinearGradient(0, y, 0, bottom)
            # Best run is highlighted green, the rest cyan -- matching the design's two bar colours.
            if value <= best + 0.01:
                gradient.setColorAt(0, QColor(theme.ACCENT)); gradient.setColorAt(1, QColor(theme.ACCENT_DIM))
            else:
                gradient.setColorAt(0, QColor(theme.CYAN)); gradient.setColorAt(1, QColor(theme.CYAN_DIM))
            painter.setBrush(QBrush(gradient)); painter.setPen(Qt.NoPen)
            painter.drawRoundedRect(x, y, width, height, 3, 3)
            painter.setPen(QColor(theme.TEXT_DIM))
            painter.setFont(QFont(theme.mono_family(), 7))
            painter.drawText(int(x - 6), int(bottom + 15), int(width + 12), 12,
                             Qt.AlignCenter, f"{value:.0f}s")
        painter.end()


class ProfileCard(QFrame):
    """`card-left` / `card-meta` / `card-right` from the Home frame."""

    def __init__(self, data: dict, on_play, on_open, busy: bool) -> None:
        super().__init__()
        self.setObjectName("card")
        row = QHBoxLayout(self)
        row.setContentsMargins(16, 14, 16, 14)
        row.setSpacing(14)

        icon = QLabel("◆")
        icon.setObjectName("packIcon")
        icon.setFixedSize(46, 46)
        icon.setAlignment(Qt.AlignCenter)
        icon.setStyleSheet(f"font-size:20px;color:{theme.ACCENT};")
        row.addWidget(icon)

        left = QVBoxLayout()
        left.setSpacing(3)
        left.addWidget(label(data["name"], "cardTitle"))
        left.addWidget(label(f"FORGE {data['minecraft']}-{data['forge']}  ·  "
                             f"{data['mods']} mods", "cardMeta"))
        marks = "JAVA 26 ✓   FORGEFAST ✓" + ("   NATIVE ✓" if data.get("native") else "")
        if not data.get("auth"):
            marks += "   OFFLINE"
        left.addWidget(label(marks, "badge"))
        row.addLayout(left, 1)

        if data.get("last"):
            right = QVBoxLayout()
            right.setSpacing(1)
            right.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            right.addWidget(label(f"{data['last']:.1f}s", "metricValue"))
            right.addWidget(label("LAST STARTUP", "metricLabel"))
            row.addLayout(right)

        details = QPushButton("Details")
        details.setObjectName("ghost")
        details.clicked.connect(lambda: on_open(data["id"]))
        row.addWidget(details)

        play = QPushButton("LAUNCH PACK")
        play.setObjectName("play")
        play.setEnabled(not busy)
        play.clicked.connect(lambda: on_play(data["id"], False))
        row.addWidget(play)


class Launcher(QWidget):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("ForgeFast Launcher")
        self.resize(1180, 720)
        self.bridge = Bridge()
        self.busy = False
        self.current: str | None = None
        self.log_lines: list[str] = []

        self.mono = theme.mono_family()
        QApplication.instance().setStyleSheet(theme.stylesheet(self.mono))

        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        root.addWidget(self._sidebar())
        self.pages = QStackedWidget()
        root.addWidget(self.pages, 1)

        self.page_home = self._page_home()
        self.page_add = self._page_add()
        self.page_details = self._page_details()
        self.page_launching = self._page_launching()
        self.page_stats = self._page_stats()
        self.page_settings = self._page_settings()
        self.page_error = self._page_error()
        for page in (self.page_home, self.page_add, self.page_details, self.page_launching,
                     self.page_stats, self.page_settings, self.page_error):
            self.pages.addWidget(page)

        self.bridge.line.connect(self._on_line)
        self.bridge.finished.connect(self._on_finished)
        self.bridge.auth_step.connect(lambda text: self.auth_status.setText(text))
        self.bridge.auth_done.connect(self._on_auth_done)
        self.refresh()

    # ---------- chrome ----------

    def _sidebar(self) -> QWidget:
        bar = QWidget()
        bar.setObjectName("sidebar")
        bar.setFixedWidth(210)
        column = QVBoxLayout(bar)
        column.setContentsMargins(16, 20, 16, 16)
        column.setSpacing(6)

        column.addWidget(label("FORGEFAST", "logoText"))
        column.addWidget(label("JAVA 26 · MINECRAFT 1.12.2", "logoSub"))
        column.addSpacing(22)

        self.nav = {}
        for key, text in (("home", "  Profiles"), ("stats", "  Performance"),
                          ("settings", "  Settings")):
            button = QPushButton(text)
            button.setObjectName("nav")
            button.setCheckable(True)
            button.clicked.connect(lambda _=False, k=key: self.go(k))
            column.addWidget(button)
            self.nav[key] = button
        self.nav["home"].setChecked(True)
        column.addStretch(1)

        self.auth_status = label("Not signed in", "cardMeta")
        self.auth_status.setWordWrap(True)
        column.addWidget(self.auth_status)
        self.sign_in = QPushButton("Sign in with Microsoft")
        self.sign_in.setObjectName("ghost")
        self.sign_in.clicked.connect(self._start_login)
        column.addWidget(self.sign_in)
        column.addWidget(label("BUILD v1.4.2-RELEASE", "metricLabel"))
        return bar

    def go(self, key: str) -> None:
        for name, button in self.nav.items():
            button.setChecked(name == key)
        self.pages.setCurrentWidget({"home": self.page_home, "stats": self.page_stats,
                                     "settings": self.page_settings}[key])
        self.refresh()

    def _scroll(self, inner: QWidget) -> QScrollArea:
        area = QScrollArea()
        area.setWidgetResizable(True)
        area.setWidget(inner)
        return area

    # ---------- pages ----------

    def _page_home(self) -> QWidget:
        page = QWidget()
        column = QVBoxLayout(page)
        column.setContentsMargins(26, 22, 26, 22)
        column.setSpacing(14)

        header = QHBoxLayout()
        header.addWidget(label("Saved Modpack Profiles", "", 19, True))
        header.addStretch(1)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search profiles...")
        self.search.setFixedWidth(230)
        self.search.textChanged.connect(self.refresh)
        header.addWidget(self.search)
        add = QPushButton("+  ADD PROFILE")
        add.setObjectName("play")
        add.clicked.connect(lambda: self.pages.setCurrentWidget(self.page_add))
        header.addWidget(add)
        column.addLayout(header)

        self.list_host = QWidget()
        self.list_column = QVBoxLayout(self.list_host)
        self.list_column.setContentsMargins(0, 0, 8, 0)
        self.list_column.setSpacing(10)
        self.list_column.addStretch(1)
        column.addWidget(self._scroll(self.list_host), 1)
        return page

    def _page_add(self) -> QWidget:
        page = QWidget()
        column = QVBoxLayout(page)
        column.setContentsMargins(26, 22, 26, 22)
        column.setSpacing(12)
        column.addWidget(label("CREATE NEW PROFILE", "sectionHeader"))
        column.addWidget(label("Configure parameters below.", "cardMeta"))
        column.addSpacing(10)

        column.addWidget(label("Profile Name", "metricLabel"))
        self.field_name = QLineEdit()
        self.field_name.setPlaceholderText("My Optimized 1.12.2 Modpack")
        column.addWidget(self.field_name)

        column.addWidget(label("Modpack Directory Path", "metricLabel"))
        row = QHBoxLayout()
        self.field_path = QLineEdit()
        self.field_path.setPlaceholderText("/path/to/your/1.12.2/modpack")
        row.addWidget(self.field_path, 1)
        browse = QPushButton("Browse")
        browse.setObjectName("ghost")
        browse.clicked.connect(self._browse)
        row.addWidget(browse)
        column.addLayout(row)

        self.add_error = label("", "cardMeta", color=theme.DANGER)
        self.add_error.setWordWrap(True)
        column.addWidget(self.add_error)
        column.addStretch(1)

        actions = QHBoxLayout()
        actions.addStretch(1)
        cancel = QPushButton("Cancel")
        cancel.setObjectName("ghost")
        cancel.clicked.connect(lambda: self.pages.setCurrentWidget(self.page_home))
        actions.addWidget(cancel)
        create = QPushButton("Create Profile")
        create.setObjectName("play")
        create.clicked.connect(self._create)
        actions.addWidget(create)
        column.addLayout(actions)
        return page

    def _page_details(self) -> QWidget:
        page = QWidget()
        column = QVBoxLayout(page)
        column.setContentsMargins(26, 22, 26, 22)
        column.setSpacing(14)
        back = QPushButton("←  Back")
        back.setObjectName("ghost")
        back.setFixedWidth(96)
        back.clicked.connect(lambda: self.pages.setCurrentWidget(self.page_home))
        column.addWidget(back)
        self.detail_title = label("", "", 21, True)
        column.addWidget(self.detail_title)
        self.detail_meta = label("", "cardMeta")
        column.addWidget(self.detail_meta)
        column.addWidget(label("PROFILE METRICS", "sectionHeader"))
        self.detail_grid = QGridLayout()
        self.detail_grid.setSpacing(10)
        column.addLayout(self.detail_grid)
        column.addStretch(1)

        actions = QHBoxLayout()
        open_dir = QPushButton("Open Directory")
        open_dir.setObjectName("ghost")
        open_dir.clicked.connect(self._open_dir)
        actions.addWidget(open_dir)
        safe = QPushButton("Launch in Safe Mode")
        safe.setObjectName("ghost")
        safe.clicked.connect(lambda: self._play(self.current, True))
        actions.addWidget(safe)
        actions.addStretch(1)
        delete = QPushButton("Delete Profile")
        delete.setObjectName("danger")
        delete.clicked.connect(self._delete)
        actions.addWidget(delete)
        play = QPushButton("LAUNCH PACK")
        play.setObjectName("play")
        play.clicked.connect(lambda: self._play(self.current, False))
        actions.addWidget(play)
        column.addLayout(actions)
        return page

    def _page_launching(self) -> QWidget:
        page = QWidget()
        column = QVBoxLayout(page)
        column.setContentsMargins(26, 22, 26, 22)
        column.setSpacing(10)
        column.addStretch(1)
        self.launch_title = label("LAUNCHING MINECRAFT", "", 22, True, theme.ACCENT)
        self.launch_title.setAlignment(Qt.AlignCenter)
        column.addWidget(self.launch_title)
        self.launch_step = label("Applying ForgeFast optimizations", "cardMeta")
        self.launch_step.setAlignment(Qt.AlignCenter)
        column.addWidget(self.launch_step)
        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        self.progress.setFixedHeight(6)
        column.addWidget(self.progress)
        self.log_view = QTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMinimumHeight(240)
        column.addWidget(self.log_view, 1)
        column.addStretch(1)
        return page

    def _page_stats(self) -> QWidget:
        page = QWidget()
        column = QVBoxLayout(page)
        column.setContentsMargins(26, 22, 26, 22)
        column.setSpacing(14)
        column.addWidget(label("Launch Performance & Optimizations", "", 19, True))
        column.addWidget(label("LAUNCH TIME COMPARISON OVER PAST SESSIONS", "sectionHeader"))
        self.chart = BarChart()
        column.addWidget(self.chart)
        self.stats_grid = QGridLayout()
        self.stats_grid.setSpacing(10)
        column.addLayout(self.stats_grid)
        column.addStretch(1)
        return page

    def _page_settings(self) -> QWidget:
        page = QWidget()
        column = QVBoxLayout(page)
        column.setContentsMargins(26, 22, 26, 22)
        column.setSpacing(12)
        column.addWidget(label("Launcher Configuration", "", 19, True))
        column.addWidget(label("JAVA & PERFORMANCE PREFERENCES", "sectionHeader"))

        column.addWidget(label("Java Runtime Executable Path", "metricLabel"))
        self.java_path = QLineEdit()
        self.java_path.setReadOnly(True)
        column.addWidget(self.java_path)

        self.ram_label = label("Allocated RAM", "metricLabel")
        column.addWidget(self.ram_label)
        self.ram = QSlider(Qt.Horizontal)
        self.ram.setRange(2, 24)
        self.ram.setValue(max(2, core.default_ram_mb() // 1024))
        self.ram.valueChanged.connect(self._ram_changed)
        column.addWidget(self.ram)

        column.addWidget(label("Active JVM Arguments", "metricLabel"))
        self.jvm_view = QTextEdit()
        self.jvm_view.setReadOnly(True)
        self.jvm_view.setMaximumHeight(150)
        column.addWidget(self.jvm_view)
        column.addWidget(label("Generated Java 26 optimal flags", "cardMeta"))
        column.addStretch(1)
        return page

    def _page_error(self) -> QWidget:
        page = QWidget()
        column = QVBoxLayout(page)
        column.setContentsMargins(26, 22, 26, 22)
        column.setSpacing(12)
        column.addStretch(1)
        banner = label("⚠  MINECRAFT CRASHED DURING INITIALIZATION", "", 17, True, theme.DANGER)
        banner.setAlignment(Qt.AlignCenter)
        column.addWidget(banner)
        self.error_reason = label("", "cardMeta")
        self.error_reason.setAlignment(Qt.AlignCenter)
        self.error_reason.setWordWrap(True)
        column.addWidget(self.error_reason)
        column.addWidget(label("CRASH LOG DIAGNOSTICS", "sectionHeader"))
        self.error_log = QTextEdit()
        self.error_log.setReadOnly(True)
        self.error_log.setMinimumHeight(220)
        column.addWidget(self.error_log)
        actions = QHBoxLayout()
        actions.addStretch(1)
        back = QPushButton("Back")
        back.setObjectName("ghost")
        back.clicked.connect(lambda: self.pages.setCurrentWidget(self.page_home))
        actions.addWidget(back)
        safe = QPushButton("Relaunch in Safe Mode")
        safe.setObjectName("play")
        safe.clicked.connect(lambda: self._play(self.current, True))
        actions.addWidget(safe)
        column.addLayout(actions)
        column.addStretch(1)
        return page

    # ---------- data ----------

    def _profiles(self) -> list[dict]:
        store = core.Store()
        components = core.forgefast_components()
        out = []
        for profile in store.profiles:
            history = profile.data.get("history") or []
            out.append({"id": profile.id, "name": profile.name, "path": str(profile.path),
                        "minecraft": profile.data.get("minecraft") or "?",
                        "forge": profile.data.get("forge") or "?",
                        "mods": profile.data.get("mods") or 0,
                        "last": history[-1] if history else None, "history": history,
                        "auth": core.load_auth(profile.id) is not None,
                        "native": components.get("coremod") is not None})
        return out

    def refresh(self) -> None:
        profiles = self._profiles()
        needle = self.search.text().strip().lower() if hasattr(self, "search") else ""
        while self.list_column.count() > 1:
            item = self.list_column.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        shown = [p for p in profiles if needle in p["name"].lower()]
        if not shown:
            empty = label("No profiles yet — add your Forge 1.12.2 modpack folder.", "cardMeta")
            empty.setAlignment(Qt.AlignCenter)
            self.list_column.insertWidget(0, empty)
        for index, data in enumerate(shown):
            self.list_column.insertWidget(index, ProfileCard(data, self._play, self._open, self.busy))

        java = core.find_java26()
        if hasattr(self, "java_path"):
            self.java_path.setText(str(java) if java else "Java 26 not found")
            self._ram_changed(self.ram.value())
        signed = any(p["auth"] for p in profiles)
        self.auth_status.setText("Signed in ✓" if signed else "Not signed in — singleplayer only")
        self.auth_status.setStyleSheet(f"color:{theme.ACCENT if signed else theme.TEXT_DIM};font-size:11px")

        if hasattr(self, "chart"):
            history = profiles[0]["history"] if profiles else []
            self.chart.set_values([float(v) for v in history])
            self._fill(self.stats_grid, [
                ("Avg Launch Time", f"{sum(history)/len(history):.1f}s" if history else "—"),
                ("Best Launch", f"{min(history):.1f}s" if history else "—"),
                ("Sessions", str(len(history))),
                ("VS 45.2s DEFAULT", "ForgeFast ON"),
            ])

    def _fill(self, grid: QGridLayout, pairs: list[tuple[str, str]]) -> None:
        while grid.count():
            item = grid.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        for index, (name, value) in enumerate(pairs):
            box = QFrame()
            box.setObjectName("card")
            inner = QVBoxLayout(box)
            inner.setContentsMargins(14, 12, 14, 12)
            inner.setSpacing(2)
            inner.addWidget(label(value, "metricValue"))
            inner.addWidget(label(name.upper(), "metricLabel"))
            grid.addWidget(box, index // 4, index % 4)

    def _ram_changed(self, gigabytes: int) -> None:
        self.ram_label.setText(f"Allocated RAM   —   MEM ALLOCATED: {gigabytes}.0 GB")
        store = core.Store()
        if store.profiles:
            profile = store.profiles[0]
            profile.data["ramMb"] = gigabytes * 1024
            store.save()
            try:
                argv = core.build_command(profile, core.forgefast_components())
                flags = [a for a in argv if a.startswith("-X") or a.startswith("-D")]
                self.jvm_view.setPlainText("\n".join(flags))
            except Exception as failure:
                self.jvm_view.setPlainText(str(failure))

    # ---------- actions ----------

    def _browse(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Select your Forge 1.12.2 modpack folder")
        if folder:
            self.field_path.setText(folder)
            if not self.field_name.text():
                self.field_name.setText(Path(folder).name)

    def _create(self) -> None:
        store = core.Store()
        path = Path(self.field_path.text().strip()).expanduser()
        profile = core.Profile.create(self.field_name.text().strip() or path.name, path)
        problems = core.analyse(profile)
        if problems:
            self.add_error.setText("  ·  ".join(problems))
            return
        store.profiles.append(profile)
        store.save()
        self.add_error.setText("")
        self.field_name.clear()
        self.field_path.clear()
        self.pages.setCurrentWidget(self.page_home)
        self.refresh()

    def _open(self, profile_id: str) -> None:
        self.current = profile_id
        data = next((p for p in self._profiles() if p["id"] == profile_id), None)
        if not data:
            return
        self.detail_title.setText(data["name"])
        self.detail_meta.setText(f"FORGE {data['minecraft']}-{data['forge']}  ·  {data['path']}")
        history = data["history"]
        self._fill(self.detail_grid, [
            ("Mods Installed", str(data["mods"])),
            ("Last Played", f"{data['last']:.1f}s" if data["last"] else "Never"),
            ("Sessions", str(len(history))),
            ("Best Launch", f"{min(history):.1f}s" if history else "—"),
        ])
        self.pages.setCurrentWidget(self.page_details)

    def _open_dir(self) -> None:
        data = next((p for p in self._profiles() if p["id"] == self.current), None)
        if data:
            from PySide6.QtGui import QDesktopServices
            from PySide6.QtCore import QUrl
            QDesktopServices.openUrl(QUrl.fromLocalFile(data["path"]))

    def _delete(self) -> None:
        if QMessageBox.question(self, "Delete profile",
                                "Remove this profile from the launcher?\n\n"
                                "Your modpack folder, saves, configs and mods are NOT deleted."
                                ) != QMessageBox.Yes:
            return
        store = core.Store()
        target = next((p for p in store.profiles if p.id == self.current), None)
        if target:
            store.profiles.remove(target)
            store.save()
        self.pages.setCurrentWidget(self.page_home)
        self.refresh()

    def _play(self, profile_id: str | None, safe: bool) -> None:
        if self.busy or not profile_id:
            return
        store = core.Store()
        profile = next((p for p in store.profiles if p.id == profile_id), None)
        if profile is None:
            return
        problems = core.analyse(profile)
        if problems:
            QMessageBox.warning(self, "Cannot launch", "\n\n".join(problems))
            return
        self.busy = True
        self.current = profile_id
        self.log_lines = []
        self.log_view.clear()
        self.launch_title.setText("LAUNCHING MINECRAFT" + ("  ·  SAFE MODE" if safe else ""))
        self.pages.setCurrentWidget(self.page_launching)
        threading.Thread(target=core.launch, daemon=True, args=(
            profile, core.forgefast_components(),
            self.bridge.line.emit,
            lambda ready, code, reason: self.bridge.finished.emit(ready, code, reason),
            safe)).start()

    def _on_line(self, text: str) -> None:
        self.log_lines.append(text)
        if len(self.log_lines) > 400:
            del self.log_lines[:200]
        # A step label that tracks the real log, rather than a fake animation.
        lowered = text.lower()
        for needle, step in (("coremod", "Injecting parallel modloader patches..."),
                             ("discovery", "Building discovery cache..."),
                             ("resource index", "Indexing pack resources..."),
                             ("texture", "Stitching texture atlas..."),
                             ("successfully loaded", "Finalising...")):
            if needle in lowered:
                self.launch_step.setText(step)
                break
        self.log_view.setPlainText("\n".join(self.log_lines[-200:]))
        self.log_view.verticalScrollBar().setValue(self.log_view.verticalScrollBar().maximum())

    def _on_finished(self, ready, code: int, reason: str) -> None:
        self.busy = False
        if ready is not None:
            store = core.Store()
            profile = next((p for p in store.profiles if p.id == self.current), None)
            if profile is not None:
                profile.data.setdefault("history", []).append(round(float(ready), 2))
                profile.data["history"] = profile.data["history"][-20:]
                store.save()
            self.pages.setCurrentWidget(self.page_home)
        else:
            self.error_reason.setText(reason)
            self.error_log.setPlainText("\n".join(self.log_lines[-120:]))
            self.pages.setCurrentWidget(self.page_error)
        self.refresh()

    # ---------- microsoft sign-in ----------

    def _start_login(self) -> None:
        store = core.Store()
        if not store.profiles:
            QMessageBox.information(self, "Sign in", "Add a profile first.")
            return
        profile = store.profiles[0]
        client = core.client_id()
        if not client:
            QMessageBox.information(
                self, "Microsoft sign-in needs a client ID",
                "Microsoft requires an Azure application ID to identify this launcher.\n\n"
                "1. portal.azure.com -> App registrations -> New registration\n"
                "2. Supported accounts: personal Microsoft accounts\n"
                "3. Platform: Mobile and desktop applications\n"
                "4. Authentication -> enable 'Allow public client flows'\n\n"
                "Then run:  ffl.py login --set-client-id <ID>")
            return
        self.sign_in.setEnabled(False)

        def work() -> None:
            import msauth
            try:
                device = msauth.begin_device_login(client)
                self.bridge.auth_step.emit(f"Code {device['user_code']} — approve in browser")
                try:
                    import webbrowser
                    webbrowser.open(device["verification_uri"])
                except Exception:
                    pass
                token = msauth.poll_for_token(client, device)
                session = msauth.minecraft_session(token["access_token"])
                core.save_auth(profile.id, session["username"], session["uuid"],
                               session["accessToken"])
                if token.get("refresh_token"):
                    core.save_refresh(profile.id, token["refresh_token"])
                self.bridge.auth_done.emit(True, session["username"])
            except Exception as failure:
                self.bridge.auth_done.emit(False, str(failure))

        threading.Thread(target=work, daemon=True).start()

    def _on_auth_done(self, ok: bool, detail: str) -> None:
        self.sign_in.setEnabled(True)
        if ok:
            QMessageBox.information(self, "Signed in", f"Signed in as {detail}.")
        else:
            QMessageBox.warning(self, "Sign-in failed", detail)
        self.refresh()


def main() -> int:
    if core.harness is None:
        print(f"Launch backend failed to import: {core._HARNESS_IMPORT_ERROR}", file=sys.stderr)
        return 2
    app = QApplication(sys.argv)
    window = Launcher()
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
