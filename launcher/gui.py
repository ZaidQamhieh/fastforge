#!/usr/bin/env python3
"""ForgeFast Launcher GUI.

Tkinter, from the standard library, so the application runs without pip, wheels, or a packaging
toolchain. The trade is visual polish for working today, which is the right trade for v1.

Launching happens on a worker thread and output is marshalled back through a queue, because Tk is not
thread-safe and Minecraft writes for several minutes -- doing it inline would freeze the window for
the entire startup.
"""
from __future__ import annotations

import queue
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

sys.path.insert(0, str(Path(__file__).resolve().parent))
import forgefast_launcher as core
from forgefast_launcher import forgefast_components




class LauncherWindow(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("ForgeFast Launcher")
        self.geometry("760x560")
        self.store = core.Store()
        self.messages: queue.Queue = queue.Queue()
        self.running = False
        self._build()
        self._refresh()
        self.after(100, self._drain)

    def _build(self) -> None:
        header = ttk.Frame(self, padding=12)
        header.pack(fill="x")
        ttk.Label(header, text="ForgeFast Launcher",
                  font=("TkDefaultFont", 16, "bold")).pack(side="left")
        java = core.find_java26()
        status = f"Java 26: {java}" if java else "Java 26: NOT FOUND"
        ttk.Label(header, text=status, foreground="#2a7" if java else "#c33").pack(side="right")

        body = ttk.Frame(self, padding=(12, 0))
        body.pack(fill="both", expand=True)

        self.tree = ttk.Treeview(body, columns=("mc", "forge", "mods", "last"), height=8)
        self.tree.heading("#0", text="Profile")
        for key, label, width in (("mc", "Minecraft", 90), ("forge", "Forge", 130),
                                  ("mods", "Mods", 60), ("last", "Last startup", 110)):
            self.tree.heading(key, text=label)
            self.tree.column(key, width=width, anchor="center")
        self.tree.pack(fill="both", expand=True)

        buttons = ttk.Frame(body, padding=(0, 8))
        buttons.pack(fill="x")
        self.play_button = ttk.Button(buttons, text="Play", command=self._play)
        self.play_button.pack(side="left")
        ttk.Button(buttons, text="Safe Mode",
                   command=lambda: self._play(safe=True)).pack(side="left", padx=4)
        ttk.Button(buttons, text="Add Profile", command=self._add).pack(side="left", padx=4)
        ttk.Button(buttons, text="Remove", command=self._remove).pack(side="left", padx=4)

        ttk.Label(body, text="Output").pack(anchor="w")
        self.output = tk.Text(body, height=12, wrap="none", state="disabled")
        self.output.pack(fill="both", expand=True, pady=(2, 10))

    def _refresh(self) -> None:
        self.tree.delete(*self.tree.get_children())
        for profile in self.store.profiles:
            history = profile.data.get("history") or []
            last = f"{history[-1]:.1f} s" if history else "-"
            self.tree.insert("", "end", iid=profile.id, text=profile.name,
                             values=(profile.data.get("minecraft") or "?",
                                     profile.data.get("forge") or "?",
                                     profile.data.get("mods") or 0, last))

    def _selected(self) -> core.Profile | None:
        focus = self.tree.focus()
        return next((p for p in self.store.profiles if p.id == focus), None)

    def _add(self) -> None:
        folder = filedialog.askdirectory(title="Select your Forge 1.12.2 modpack folder")
        if not folder:
            return
        name = Path(folder).name
        profile = core.Profile.create(name, Path(folder))
        problems = core.analyse(profile)
        if problems:
            messagebox.showerror("Cannot use this folder", "\n\n".join(problems))
            return
        self.store.profiles.append(profile)
        self.store.save()
        self._refresh()
        self._log(f"Added '{profile.name}': Minecraft {profile.data['minecraft']}, "
                  f"Forge {profile.data['forge']}, {profile.data['mods']} mods")

    def _remove(self) -> None:
        profile = self._selected()
        if profile is None:
            return
        # The user's modpack is never touched; only launcher metadata goes.
        if not messagebox.askyesno("Remove profile",
                                   f"Remove '{profile.name}' from the launcher?\n\n"
                                   "Your modpack folder, saves, configs and mods are NOT deleted."):
            return
        self.store.profiles.remove(profile)
        self.store.save()
        self._refresh()

    def _play(self, safe: bool = False) -> None:
        if self.running:
            messagebox.showinfo("Already running", "A game is already starting.")
            return
        profile = self._selected()
        if profile is None:
            messagebox.showinfo("No profile", "Select a profile first.")
            return
        problems = core.analyse(profile)
        if problems:
            messagebox.showerror("Profile not launchable", "\n\n".join(problems))
            return

        components = forgefast_components()
        if not components["artifacts"]:
            self._log("ForgeFast jars not found -- launching stock Forge on Java 26.")
        else:
            self._log(f"ForgeFast active ({len(components['artifacts'])} substituted jars)"
                      + (" [SAFE MODE]" if safe else ""))

        self.running = True
        self.play_button.state(["disabled"])
        self._log(f"Launching '{profile.name}'...")
        threading.Thread(target=core.launch, daemon=True, args=(
            profile, components,
            lambda line: self.messages.put(("line", line)),
            lambda ready, code, reason: self.messages.put(("done", (profile.id, ready, code, reason))),
            safe,
        )).start()

    def _drain(self) -> None:
        try:
            while True:
                kind, payload = self.messages.get_nowait()
                if kind == "line":
                    self._log(payload)
                else:
                    profile_id, ready, code, reason = payload
                    profile = next((p for p in self.store.profiles if p.id == profile_id), None)
                    if profile is not None and ready is not None:
                        profile.data.setdefault("history", []).append(round(ready, 2))
                        profile.data["history"] = profile.data["history"][-20:]
                    if profile is not None:
                        profile.data["lastError"] = None if code == 0 else reason
                    self.store.save()
                    self._refresh()
                    self._log(reason)
                    self.running = False
                    self.play_button.state(["!disabled"])
        except queue.Empty:
            pass
        self.after(100, self._drain)

    def _log(self, text: str) -> None:
        self.output.configure(state="normal")
        self.output.insert("end", text + "\n")
        # Bounded, or a multi-minute startup fills memory with log lines.
        if int(self.output.index("end-1c").split(".")[0]) > 800:
            self.output.delete("1.0", "200.0")
        self.output.see("end")
        self.output.configure(state="disabled")


def main() -> int:
    if core.harness is None:
        print(f"Cannot start: the launch backend failed to import: {core._HARNESS_IMPORT_ERROR}",
              file=sys.stderr)
        return 2
    LauncherWindow().mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
