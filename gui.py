"""gui.py - tkinter desktop front-end for yt2disc.

Every job goes through :mod:`core` and :mod:`player`, so the GUI and the CLI
behave identically.  Slow jobs (scanning folders, decoding a track) run on
worker threads and report back through a queue that is polled with
``root.after`` so all widget access stays on the main thread.

The window is three tabs:

    Player      - files and folders from this device, play / random / stop
    Playlists   - build and edit playlists
    Soundtrack  - Minecraft's own music, read out of the game's files
"""

from __future__ import annotations

import os
import queue
import sys
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk

import core
import player as player_module

APP_TITLE = "yt2disc  -  Minecraft music player"
POLL_MS = 60
CATEGORY_ALL = "all"


class MusicApp:
    """The whole application window."""

    def __init__(self, root: tk.Tk):
        self.root = root
        self.events: "queue.Queue[tuple]" = queue.Queue()
        self.busy = False
        self.tracks: list[dict] = []
        self.soundtracks: list[dict] = []

        root.title(APP_TITLE)
        root.geometry("1060x760")
        root.minsize(900, 620)

        self.status_var = tk.StringVar(value="Ready.")
        self.now_var = tk.StringVar(value="Nothing playing.")
        self.mute_var = tk.BooleanVar(value=core.mute_preference())
        self.category_var = tk.StringVar(value=CATEGORY_ALL)
        self.search_var = tk.StringVar()
        self.progress_var = tk.DoubleVar(value=0.0)

        self.engine = player_module.Player(
            log=self._log,
            on_event=self._on_player_event,
            mute_minecraft=self.mute_var.get(),
        )

        self._install_theme()
        self._build_menu()
        self._build_widgets()
        self.refresh_library()
        self.refresh_playlists()
        self.load_soundtrack()
        self.root.after(POLL_MS, self._drain)
        self.root.after(300, self._announce_ffmpeg)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ------------------------------------------------------------------
    # look and menus
    # ------------------------------------------------------------------
    def _install_theme(self) -> None:
        style = ttk.Style(self.root)
        for theme in ("clam", "vista", "winnative", "default"):
            if theme in style.theme_names():
                try:
                    style.theme_use(theme)
                    break
                except tk.TclError:
                    continue
        style.configure("Header.TLabel", font=("Segoe UI", 10, "bold"))
        style.configure("Status.TLabel", padding=(6, 3))
        style.configure("Now.TLabel", font=("Segoe UI", 10, "bold"))

    def _build_menu(self) -> None:
        menubar = tk.Menu(self.root)

        file_menu = tk.Menu(menubar, tearoff=0)
        file_menu.add_command(label="Add files...", command=self._on_add_files)
        file_menu.add_command(label="Add folder...", command=self._on_add_folder)
        file_menu.add_separator()
        file_menu.add_command(
            label="Open cache folder", command=lambda: core.open_folder(core.CACHE_DIR)
        )
        file_menu.add_command(
            label="Open bin folder", command=lambda: core.open_folder(core.BIN_DIR)
        )
        file_menu.add_separator()
        file_menu.add_command(label="Exit", command=self._on_close)
        menubar.add_cascade(label="File", menu=file_menu)

        tools_menu = tk.Menu(menubar, tearoff=0)
        tools_menu.add_command(
            label="Check helper programs", command=self._check_binaries
        )
        tools_menu.add_command(
            label="Show playlists.json", command=self._show_playlists_file
        )
        tools_menu.add_separator()
        tools_menu.add_command(
            label="Minecraft: silence music now", command=self._on_mc_off
        )
        tools_menu.add_command(
            label="Minecraft: switch music back on", command=self._on_mc_on
        )
        tools_menu.add_command(
            label="Minecraft: restore from backup", command=self._on_mc_restore
        )
        tools_menu.add_separator()
        tools_menu.add_command(label="Clear decode cache", command=self._on_clear_cache)
        menubar.add_cascade(label="Tools", menu=tools_menu)

        help_menu = tk.Menu(menubar, tearoff=0)
        help_menu.add_command(label="About yt2disc", command=self._show_about)
        menubar.add_cascade(label="Help", menu=help_menu)

        self.root.config(menu=menubar)

    # ------------------------------------------------------------------
    # widgets
    # ------------------------------------------------------------------
    def _build_widgets(self) -> None:
        self.notebook = ttk.Notebook(self.root)
        self.notebook.pack(fill="both", expand=True, padx=10, pady=(10, 4))

        self.player_tab = ttk.Frame(self.notebook)
        self.playlist_tab = ttk.Frame(self.notebook)
        self.soundtrack_tab = ttk.Frame(self.notebook)
        self.notebook.add(self.player_tab, text="Player")
        self.notebook.add(self.playlist_tab, text="Playlists")
        self.notebook.add(self.soundtrack_tab, text="Minecraft Soundtrack")

        self._build_player_tab()
        self._build_playlist_tab()
        self._build_soundtrack_tab()
        self._build_status()

    def _make_track_tree(self, parent, height=8):
        """A treeview listing tracks; returns (frame, tree)."""
        frame = ttk.Frame(parent)
        columns = ("index", "name", "length", "source", "folder")
        tree = ttk.Treeview(frame, columns=columns, show="headings", height=height)
        headings = {
            "index": ("#", 40, "center"),
            "name": ("Title", 260, "w"),
            "length": ("Length", 70, "center"),
            "source": ("Source", 90, "center"),
            "folder": ("Folder", 380, "w"),
        }
        for key, (text, width, anchor) in headings.items():
            tree.heading(key, text=text)
            tree.column(key, width=width, anchor=anchor, stretch=(key == "folder"))
        tree.pack(side="left", fill="both", expand=True)
        scroll = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
        scroll.pack(side="right", fill="y")
        tree.configure(yscrollcommand=scroll.set)
        tree.bind("<Double-Button-1>", lambda _event: self._on_play_selected())
        return frame, tree

    def _fill_tree(self, tree, tracks) -> None:
        tree.delete(*tree.get_children())
        for index, track in enumerate(tracks, 1):
            source = "minecraft" if track.get("vanilla") else "file"
            if not track.get("exists", True):
                source = "missing"
            tree.insert(
                "",
                "end",
                iid=str(index - 1),
                values=(
                    index,
                    track.get("name", "?"),
                    track.get("duration_text", "--:--"),
                    source,
                    track.get("folder", ""),
                ),
            )

    def _selected_track(self, tree, tracks):
        selection = tree.selection()
        if not selection:
            return None
        try:
            return tracks[int(selection[0])]
        except (ValueError, IndexError):
            return None

    # ------------------------------------------------------------------
    # tab 1: player
    # ------------------------------------------------------------------
    def _build_player_tab(self) -> None:
        outer = ttk.Frame(self.player_tab)
        outer.pack(fill="both", expand=True)

        header = ttk.Frame(outer)
        header.pack(fill="x", padx=10, pady=(10, 4))
        ttk.Label(
            header, text="Music on this device", style="Header.TLabel"
        ).pack(side="left")
        ttk.Button(header, text="Add files...", command=self._on_add_files).pack(
            side="left", padx=(12, 4)
        )
        ttk.Button(header, text="Add folder...", command=self._on_add_folder).pack(
            side="left", padx=4
        )
        ttk.Button(header, text="Rescan library", command=self.refresh_library).pack(
            side="left", padx=4
        )
        ttk.Button(header, text="Clear list", command=self._on_clear_list).pack(
            side="left", padx=4
        )

        list_frame = ttk.LabelFrame(outer, text="Tracks")
        list_frame.pack(fill="both", expand=True, padx=10, pady=4)
        tree_row, self.track_tree = self._make_track_tree(list_frame, height=10)
        tree_row.pack(fill="both", expand=True, padx=8, pady=8)

        controls = ttk.LabelFrame(outer, text="Playback")
        controls.pack(fill="x", padx=10, pady=(4, 8))

        row = ttk.Frame(controls)
        row.pack(fill="x", padx=8, pady=(8, 2))
        ttk.Button(row, text="Play selected", command=self._on_play_selected).pack(
            side="left"
        )
        self.random_btn = ttk.Button(
            row, text="Play random", command=self._on_play_random
        )
        self.random_btn.pack(side="left", padx=6)
        self.next_btn = ttk.Button(row, text="Next", command=self._on_play_next)
        self.next_btn.pack(side="left", padx=6)
        self.stop_btn = ttk.Button(row, text="Stop", command=self._on_stop)
        self.stop_btn.pack(side="left", padx=6)
        ttk.Button(row, text="Reveal file", command=self._reveal_selected).pack(
            side="right"
        )

        ttk.Checkbutton(
            controls,
            text="Mute Minecraft's background music while yt2disc is playing",
            variable=self.mute_var,
            command=self._on_mute_toggled,
        ).pack(anchor="w", padx=8, pady=(2, 10))

        now = ttk.Frame(outer)
        now.pack(fill="x", padx=12, pady=(0, 8))
        ttk.Label(now, textvariable=self.now_var, style="Now.TLabel").pack(
            anchor="w"
        )

    # ------------------------------------------------------------------
    # tab 2: playlists
    # ------------------------------------------------------------------
    def _build_playlist_tab(self) -> None:
        outer = ttk.Frame(self.playlist_tab)
        outer.pack(fill="both", expand=True)

        left = ttk.LabelFrame(outer, text="Playlists")
        left.pack(side="left", fill="y", padx=(10, 6), pady=10)

        self.playlist_box = tk.Listbox(
            left, width=28, exportselection=False, height=16
        )
        self.playlist_box.pack(fill="both", expand=True, padx=8, pady=(8, 4))
        self.playlist_box.bind(
            "<<ListboxSelect>>", lambda _event: self.show_playlist()
        )

        playlist_buttons = ttk.Frame(left)
        playlist_buttons.pack(fill="x", padx=8, pady=(0, 8))
        ttk.Button(playlist_buttons, text="New...", command=self._on_new_playlist).pack(
            side="left"
        )
        ttk.Button(
            playlist_buttons, text="Rename...", command=self._on_rename_playlist
        ).pack(side="left", padx=4)
        ttk.Button(
            playlist_buttons, text="Delete", command=self._on_delete_playlist
        ).pack(side="left", padx=4)

        right = ttk.LabelFrame(outer, text="Tracks in the selected playlist")
        right.pack(side="left", fill="both", expand=True, padx=(0, 10), pady=10)

        tree_row, self.playlist_tree = self._make_track_tree(right, height=12)
        tree_row.pack(fill="both", expand=True, padx=8, pady=8)

        buttons = ttk.Frame(right)
        buttons.pack(fill="x", padx=8, pady=(0, 8))
        ttk.Button(
            buttons, text="Add files...", command=self._on_playlist_add_files
        ).pack(side="left")
        ttk.Button(
            buttons, text="Add folder...", command=self._on_playlist_add_folder
        ).pack(side="left", padx=4)
        ttk.Button(
            buttons,
            text="Add from soundtrack",
            command=self._on_playlist_add_soundtrack,
        ).pack(side="left", padx=4)
        ttk.Button(buttons, text="Remove", command=self._on_playlist_remove).pack(
            side="left", padx=4
        )
        ttk.Button(buttons, text="Shuffle", command=self._on_playlist_shuffle).pack(
            side="right"
        )
        ttk.Button(buttons, text="Play", command=self._on_playlist_play).pack(
            side="right", padx=6
        )

    # ------------------------------------------------------------------
    # tab 3: Minecraft soundtrack
    # ------------------------------------------------------------------
    def _build_soundtrack_tab(self) -> None:
        outer = ttk.Frame(self.soundtrack_tab)
        outer.pack(fill="both", expand=True)

        header = ttk.Frame(outer)
        header.pack(fill="x", padx=10, pady=(10, 4))
        ttk.Label(
            header,
            text="Minecraft's own music, read from the game's files",
            style="Header.TLabel",
        ).pack(side="left")

        filters = ttk.Frame(outer)
        filters.pack(fill="x", padx=10, pady=(0, 4))
        ttk.Label(filters, text="Category:").pack(side="left")
        self.category_combo = ttk.Combobox(
            filters, textvariable=self.category_var, state="readonly", width=12
        )
        self.category_combo.pack(side="left", padx=(4, 10))
        self.category_combo.bind(
            "<<ComboboxSelected>>", lambda _event: self.load_soundtrack()
        )
        ttk.Label(filters, text="Search:").pack(side="left")
        search_entry = ttk.Entry(filters, textvariable=self.search_var, width=22)
        search_entry.pack(side="left", padx=4)
        search_entry.bind("<Return>", lambda _event: self.load_soundtrack())
        ttk.Button(filters, text="Find", command=self.load_soundtrack).pack(
            side="left", padx=4
        )
        ttk.Button(filters, text="Show all", command=self._on_soundtrack_reset).pack(
            side="left", padx=4
        )

        self.soundtrack_path_var = tk.StringVar(value="")
        ttk.Label(
            outer, textvariable=self.soundtrack_path_var, foreground="#666666"
        ).pack(anchor="w", padx=14)

        tree_row, self.soundtrack_tree = self._make_track_tree(outer, height=11)
        tree_row.pack(fill="both", expand=True, padx=10, pady=4)
        self.soundtrack_tree.bind(
            "<Double-Button-1>", lambda _event: self._on_play_soundtrack_selected()
        )

        buttons = ttk.Frame(outer)
        buttons.pack(fill="x", padx=10, pady=(0, 10))
        ttk.Button(
            buttons, text="Play selected", command=self._on_play_soundtrack_selected
        ).pack(side="left")
        ttk.Button(buttons, text="Stop", command=self._on_stop).pack(
            side="left", padx=6
        )
        ttk.Label(buttons, text="Add shown to:").pack(side="left", padx=(20, 4))
        self.add_to_var = tk.StringVar()
        self.add_to_combo = ttk.Combobox(
            buttons, textvariable=self.add_to_var, state="readonly", width=24
        )
        self.add_to_combo.pack(side="left", padx=4)
        ttk.Button(
            buttons, text="Add", command=self._on_add_soundtrack_to_playlist
        ).pack(side="left", padx=4)
        ttk.Button(
            buttons, text="Add selection", command=self._on_add_soundtrack_selection
        ).pack(side="left", padx=4)

    # ------------------------------------------------------------------
    # log + status bar
    # ------------------------------------------------------------------
    def _build_status(self) -> None:
        log_frame = ttk.LabelFrame(self.root, text="Log")
        log_frame.pack(fill="x", padx=10, pady=(0, 6))
        self.log = tk.Text(
            log_frame,
            height=6,
            wrap="word",
            state="disabled",
            background="#111318",
            foreground="#e6e6e6",
            insertbackground="#e6e6e6",
        )
        self.log.pack(side="left", fill="both", expand=True, padx=(8, 0), pady=8)
        log_scroll = ttk.Scrollbar(log_frame, orient="vertical", command=self.log.yview)
        log_scroll.pack(side="right", fill="y", pady=8, padx=(0, 8))
        self.log.configure(yscrollcommand=log_scroll.set)

        status = ttk.Frame(self.root)
        status.pack(fill="x", side="bottom")
        self.progress = ttk.Progressbar(
            status, mode="determinate", maximum=100.0, variable=self.progress_var
        )
        self.progress.pack(fill="x", padx=10, pady=(0, 4))
        ttk.Label(
            status, textvariable=self.status_var, style="Status.TLabel"
        ).pack(fill="x", padx=6, pady=(0, 6))

    # ------------------------------------------------------------------
    # worker plumbing (everything UI side stays on the main thread)
    # ------------------------------------------------------------------
    def _log(self, message) -> None:
        self.events.put(("log", str(message)))

    def _on_player_event(self, event, **payload) -> None:
        # Called from the audio thread: never touch a widget here.
        self.events.put(("player", (event, payload)))

    def _run_task(self, label, work, on_done=None) -> None:
        """Run *work* off the UI thread; ``on_done(result)`` runs on the UI thread."""
        self.status_var.set(label)
        self.busy = True

        def runner():
            try:
                result = work()
            except core.YT2DiscError as exc:
                self.events.put(("error", str(exc)))
            except Exception as exc:  # pragma: no cover - defensive
                self.events.put(("error", f"unexpected error: {exc}"))
            else:
                self.events.put(("done", (result, on_done)))

        threading.Thread(target=runner, name="yt2disc-task", daemon=True).start()

    def _drain(self) -> None:
        try:
            while True:
                kind, payload = self.events.get_nowait()
                if kind == "log":
                    self._append_log(payload)
                elif kind == "status":
                    self.status_var.set(payload)
                elif kind == "progress":
                    self.progress_var.set(payload)
                elif kind == "player":
                    self._handle_player_event(*payload)
                elif kind == "error":
                    self.busy = False
                    self.progress_var.set(0)
                    self._append_log("ERROR: " + payload)
                    self.status_var.set(payload)
                    messagebox.showerror("yt2disc", payload)
                elif kind == "done":
                    self.busy = False
                    self.progress_var.set(0)
                    result, callback = payload
                    if callback:
                        callback(result)
        except queue.Empty:
            pass
        self.root.after(POLL_MS, self._drain)

    def _handle_player_event(self, event, payload) -> None:
        track = payload.get("track") or {}
        if event == "preparing":
            self.status_var.set(f"Decoding {track.get('name')}...")
        elif event == "start":
            self.now_var.set(f"Now playing: {core.describe_track(track)}")
            self.status_var.set(f"Playing: {track.get('name')}")
            self._append_log(f"Playing {core.describe_track(track)}")
        elif event in ("finished", "stopped"):
            self.now_var.set("Nothing playing.")
            if event == "finished":
                self.status_var.set("Finished.")

    def _append_log(self, message) -> None:
        self.log.configure(state="normal")
        self.log.insert("end", str(message).rstrip() + "\n")
        # keep the widget small - only the tail matters
        if int(self.log.index("end-1c").split(".")[0]) > 400:
            self.log.delete("1.0", "100.0")
        self.log.see("end")
        self.log.configure(state="disabled")

    # ------------------------------------------------------------------
    # refreshing the lists
    # ------------------------------------------------------------------
    def refresh_library(self) -> None:
        self._run_task(
            "Scanning your library...",
            lambda: core.scan_library(log=self._log),
            self._library_loaded,
        )

    def _library_loaded(self, tracks) -> None:
        self.tracks = tracks or []
        self._fill_tree(self.track_tree, self.tracks)
        roots = core.library_roots()
        self._append_log(
            f"Library: {len(self.tracks)} track(s) in {len(roots)} folder(s)"
        )
        if self.tracks:
            self.status_var.set(
                f"{len(self.tracks)} track(s). 'Add folder...' grows the list."
            )
        else:
            self.status_var.set("No music yet - press 'Add folder...' to pick one.")

    def refresh_playlists(self, select=None) -> None:
        self.playlists = core.list_playlists()
        self.playlist_box.delete(0, "end")
        for entry in self.playlists:
            self.playlist_box.insert(
                "end", f"{entry.get('name')}  ({len(entry.get('tracks') or [])})"
            )
        names = [entry.get("name") for entry in self.playlists]
        self.add_to_combo["values"] = names
        if names and self.add_to_var.get() not in names:
            self.add_to_var.set(names[0])
        if self.playlists:
            index = 0
            if select:
                for position, entry in enumerate(self.playlists):
                    if entry.get("name") == select:
                        index = position
                        break
            self.playlist_box.selection_clear(0, "end")
            self.playlist_box.selection_set(index)
            self.playlist_box.see(index)
        self.show_playlist()

    def _selected_playlist_name(self):
        if not getattr(self, "playlists", None):
            return None
        selection = self.playlist_box.curselection()
        if not selection:
            return None
        try:
            return self.playlists[selection[0]].get("name")
        except IndexError:
            return None

    def show_playlist(self) -> None:
        name = self._selected_playlist_name()
        if not name:
            self.playlist_tracks = []
            self._fill_tree(self.playlist_tree, [])
            return
        self.playlist_tracks = core.playlist_tracks(name)
        self._fill_tree(self.playlist_tree, self.playlist_tracks)
        missing = sum(1 for track in self.playlist_tracks if not track["exists"])
        note = f", {missing} missing" if missing else ""
        self.status_var.set(f"'{name}': {len(self.playlist_tracks)} track(s){note}")

    def load_soundtrack(self) -> None:
        # Tk variables may only be read on the UI thread, so the filter values
        # are captured here and handed to the worker.
        category = self.category_var.get()
        search = self.search_var.get()
        self._run_task(
            "Reading Minecraft's soundtrack...",
            lambda: core.list_vanilla_music(category=category, search=search),
            self._soundtrack_loaded,
        )

    def _soundtrack_loaded(self, tracks) -> None:
        self.soundtracks = tracks or []
        self._fill_tree(self.soundtrack_tree, self.soundtracks)
        summary = core.soundtrack_summary()
        self.category_combo["values"] = [CATEGORY_ALL] + [
            entry["category"] for entry in summary
        ]
        root = core.vanilla_music_root()
        if root is None:
            self.soundtrack_path_var.set(
                "Minecraft's soundtrack was not found - is Minecraft for Windows "
                "installed? (YT2DISC_MINECRAFT_CONTENT overrides the search)"
            )
        else:
            self.soundtrack_path_var.set(str(root))
        self.status_var.set(f"{len(self.soundtracks)} soundtrack track(s)")

    # ------------------------------------------------------------------
    # player tab actions
    # ------------------------------------------------------------------
    def _audio_filetypes(self):
        patterns = " ".join(f"*{ext}" for ext in core.AUDIO_EXTENSIONS)
        return [("Audio files", patterns), ("All files", "*.*")]

    def _on_add_files(self) -> None:
        paths = filedialog.askopenfilenames(
            title="Add music files", filetypes=self._audio_filetypes()
        )
        if not paths:
            return
        self._run_task(
            f"Adding {len(paths)} file(s)...",
            lambda: core.resolve_tracks(list(paths)),
            self._files_added_to_list,
        )

    def _files_added_to_list(self, tracks) -> None:
        existing = {track["path"] for track in self.tracks}
        fresh = [track for track in tracks if track["path"] not in existing]
        self.tracks = sorted(
            self.tracks + fresh,
            key=lambda track: (track["name"].lower(), track["path"].lower()),
        )
        self._fill_tree(self.track_tree, self.tracks)
        self._append_log(f"Added {len(fresh)} file(s) to the list")
        self._append_log(
            "Tip: 'Add folder...' remembers a folder, so it is scanned again next time."
        )
        self.status_var.set(f"{len(self.tracks)} track(s) in the list")

    def _on_add_folder(self) -> None:
        folder = filedialog.askdirectory(title="Add a music folder")
        if not folder:
            return
        self._run_task(
            f"Scanning {folder}...",
            lambda: self._remember_folder(folder),
            self._folder_added,
        )

    def _remember_folder(self, folder):
        report = core.add_library_root(folder)
        return report, core.scan_audio_folder(folder, log=self._log)

    def _folder_added(self, result) -> None:
        report, tracks = result
        verb = "Remembered" if report["added"] else "Already remembered"
        self._append_log(f"{verb}: {report['folder']} ({len(tracks)} audio file(s))")
        self.refresh_library()

    def _on_clear_list(self) -> None:
        if not self.tracks:
            return
        if not messagebox.askyesno(
            "Clear the list",
            "Clear the track list?\n\nYour remembered folders and playlists stay as "
            "they are - press 'Rescan library' to build it again.",
        ):
            return
        self.tracks = []
        self._fill_tree(self.track_tree, [])
        self.status_var.set("List cleared - 'Rescan library' brings it back.")

    def _on_play_selected(self) -> None:
        self._play_track(self._selected_track(self.track_tree, self.tracks))

    def _play_track(self, track) -> None:
        if not track:
            messagebox.showinfo("yt2disc", "Pick a track in the list first.")
            return
        self.engine.mute_minecraft = bool(self.mute_var.get())
        self._run_task(
            f"Decoding {track.get('name')}...",
            lambda: self.engine.play(track),
            self._playing,
        )

    def _playing(self, report) -> None:
        report = report or {}
        track = report.get("track") or {}
        self.now_var.set(f"Now playing: {core.describe_track(track)}")
        label = "Random" if report.get("random") else "Playing"
        self.status_var.set(f"{label}: {track.get('name')}")
        if report.get("muted"):
            self._append_log(
                "Minecraft's background music is muted while yt2disc plays "
                "(audio_music:0) and switched back afterwards."
            )

    def _on_play_random(self) -> None:
        if not self.tracks:
            messagebox.showinfo("yt2disc", "Add some music first.")
            return
        self.engine.mute_minecraft = bool(self.mute_var.get())
        self._run_task(
            "Picking a random track...",
            lambda: self.engine.play_random(self.tracks),
            self._playing,
        )

    def _on_play_next(self) -> None:
        if not self.engine.queue:
            self.status_var.set(
                "Nothing queued - play a playlist to get a running order."
            )
            return
        self.engine.mute_minecraft = bool(self.mute_var.get())
        self._run_task("Skipping...", self.engine.play_next, self._playing_optional)

    def _playing_optional(self, report) -> None:
        if report:
            self._playing(report)
        else:
            self.status_var.set("That was the last track of the queue.")

    def _on_stop(self) -> None:
        try:
            self.engine.stop()
        except core.YT2DiscError as exc:
            messagebox.showerror("yt2disc", str(exc))
        self.now_var.set("Nothing playing.")
        self.status_var.set("Stopped.")

    def _on_mute_toggled(self) -> None:
        wanted = bool(self.mute_var.get())
        self.engine.mute_minecraft = wanted
        core.set_setting("mute_minecraft_music", wanted)
        if not wanted and self.engine.muted:
            self.engine.restore_minecraft()
            self._append_log("Minecraft's music switched back on.")
        self.status_var.set(
            "Minecraft's music will be muted while playing."
            if wanted
            else "Minecraft's music will be left alone."
        )

    def _reveal_selected(self) -> None:
        track = self._selected_track(self.track_tree, self.tracks)
        if track:
            core.reveal_in_folder(track["path"])

    # ------------------------------------------------------------------
    # playlist tab actions
    # ------------------------------------------------------------------
    def _require_playlist(self):
        name = self._selected_playlist_name()
        if not name:
            messagebox.showinfo(
                "yt2disc", "Select a playlist on the left first (or make one)."
            )
        return name

    def _on_new_playlist(self) -> None:
        name = simpledialog.askstring("New playlist", "Name for the new playlist:")
        if not name or not name.strip():
            return
        try:
            entry = core.create_playlist(name.strip())
        except core.YT2DiscError as exc:
            messagebox.showerror("yt2disc", str(exc))
            return
        self._append_log(f"Created playlist '{entry['name']}'")
        self.refresh_playlists(select=entry["name"])

    def _on_rename_playlist(self) -> None:
        name = self._require_playlist()
        if not name:
            return
        new_name = simpledialog.askstring(
            "Rename playlist", f"New name for '{name}':", initialvalue=name
        )
        if not new_name or not new_name.strip() or new_name.strip() == name:
            return
        try:
            entry = core.rename_playlist(name, new_name.strip())
        except core.YT2DiscError as exc:
            messagebox.showerror("yt2disc", str(exc))
            return
        self._append_log(f"Renamed '{name}' to '{entry['name']}'")
        self.refresh_playlists(select=entry["name"])

    def _on_delete_playlist(self) -> None:
        name = self._require_playlist()
        if not name:
            return
        if not messagebox.askyesno(
            "Delete playlist",
            f"Delete the playlist '{name}'?\n\nThe audio files themselves are kept.",
        ):
            return
        core.delete_playlist(name)
        self._append_log(f"Deleted playlist '{name}'")
        self.refresh_playlists()

    def _on_playlist_add_files(self) -> None:
        name = self._require_playlist()
        if not name:
            return
        paths = filedialog.askopenfilenames(
            title=f"Add files to '{name}'", filetypes=self._audio_filetypes()
        )
        if not paths:
            return
        self._add_paths_to_playlist(name, list(paths))

    def _on_playlist_add_folder(self) -> None:
        name = self._require_playlist()
        if not name:
            return
        folder = filedialog.askdirectory(title=f"Add a folder to '{name}'")
        if not folder:
            return
        self._run_task(
            f"Scanning {folder}...",
            lambda: core.add_to_playlist(
                name, [track["path"] for track in core.scan_audio_folder(folder)]
            ),
            lambda report: self._playlist_changed(report, name),
        )

    def _add_paths_to_playlist(self, name, paths) -> None:
        def work():
            tracks = core.resolve_tracks(paths)
            return core.add_to_playlist(name, [track["path"] for track in tracks])

        self._run_task(f"Adding to '{name}'...", work,
                       lambda report: self._playlist_changed(report, name))

    def _playlist_changed(self, report, name) -> None:
        report = report or {}
        self._append_log(
            f"'{name}': added {len(report.get('added') or [])}, "
            f"already there {len(report.get('skipped') or [])}, "
            f"total {report.get('total')}"
        )
        for path in report.get("skipped") or []:
            self._append_log(f"  already in the list: {os.path.basename(path)}")
        self.refresh_playlists(select=name)

    def _on_playlist_remove(self) -> None:
        name = self._require_playlist()
        if not name:
            return
        selection = self.playlist_tree.selection()
        if not selection:
            messagebox.showinfo("yt2disc", "Select a track of the playlist first.")
            return
        index = int(selection[0]) + 1
        try:
            report = core.remove_from_playlist(name, str(index))
        except core.YT2DiscError as exc:
            messagebox.showerror("yt2disc", str(exc))
            return
        self._append_log(
            f"Removed '{os.path.basename(report['removed'])}' from '{name}'"
        )
        self.refresh_playlists(select=name)

    def _on_playlist_play(self) -> None:
        self._play_playlist(shuffle=False)

    def _on_playlist_shuffle(self) -> None:
        self._play_playlist(shuffle=True)

    def _play_playlist(self, shuffle: bool) -> None:
        name = self._require_playlist()
        if not name:
            return
        self.engine.mute_minecraft = bool(self.mute_var.get())
        self._run_task(
            f"{'Shuffling' if shuffle else 'Playing'} '{name}'...",
            lambda: self.engine.play_playlist(name, shuffle=shuffle),
            self._playing,
        )

    def _on_playlist_add_soundtrack(self) -> None:
        name = self._require_playlist()
        if not name:
            return
        track = self._selected_track(self.soundtrack_tree, self.soundtracks)
        if not track:
            messagebox.showinfo(
                "yt2disc", "Pick a track in the Soundtrack tab first."
            )
            return
        self._add_paths_to_playlist(name, [track["path"]])

    # ------------------------------------------------------------------
    # soundtrack tab actions
    # ------------------------------------------------------------------
    def _on_soundtrack_reset(self) -> None:
        self.category_var.set(CATEGORY_ALL)
        self.search_var.set("")
        self.load_soundtrack()

    def _on_play_soundtrack_selected(self) -> None:
        track = self._selected_track(self.soundtrack_tree, self.soundtracks)
        if not track:
            messagebox.showinfo("yt2disc", "Pick one of Minecraft's tracks first.")
            return
        self._play_track(track)

    def _on_add_soundtrack_to_playlist(self) -> None:
        name = self.add_to_var.get().strip()
        if not name:
            messagebox.showinfo(
                "yt2disc", "Make a playlist first (Playlists tab), then pick it here."
            )
            return
        if not self.soundtracks:
            messagebox.showinfo("yt2disc", "Nothing is shown - nothing to add.")
            return
        self._add_paths_to_playlist(name, [track["path"] for track in self.soundtracks])

    def _on_add_soundtrack_selection(self) -> None:
        name = self.add_to_var.get().strip()
        if not name:
            messagebox.showinfo(
                "yt2disc", "Make a playlist first (Playlists tab), then pick it here."
            )
            return
        selection = self.soundtrack_tree.selection()
        if not selection:
            messagebox.showinfo("yt2disc", "Select a track to add.")
            return
        picked = [
            self.soundtracks[int(item)]
            for item in selection
            if int(item) < len(self.soundtracks)
        ]
        self._add_paths_to_playlist(name, [track["path"] for track in picked])

    # ------------------------------------------------------------------
    # misc commands
    # ------------------------------------------------------------------
    def _announce_ffmpeg(self) -> None:
        missing = core.missing_binaries()
        if missing:
            self._append_log("ffmpeg was not found - tracks cannot be decoded yet.")
            messagebox.showwarning("ffmpeg missing", core.binary_help_text())
        else:
            self._append_log("ffmpeg found - ready to play.")
        content = core.find_minecraft_content()
        self._append_log(
            "Minecraft soundtrack: "
            + (str(content) if content else "not found (is Minecraft installed?)")
        )

    def _check_binaries(self) -> None:
        messagebox.showinfo(
            "Helper programs",
            "\n".join(
                [
                    f"ffmpeg        : {core.find_binary('ffmpeg') or 'NOT FOUND'}",
                    f"soundtrack    : {core.vanilla_music_root() or 'NOT FOUND'}",
                    f"options.txt   : {core.find_options_file() or 'NOT FOUND'}",
                    f"data folder   : {core.SCRIPT_DIR}",
                    f"playlists     : {core.PLAYLISTS_PATH}",
                ]
            ),
        )

    def _show_playlists_file(self) -> None:
        if core.PLAYLISTS_PATH.is_file():
            core.open_folder(core.PLAYLISTS_PATH)
        else:
            messagebox.showinfo(
                "yt2disc", "There is no playlists.json yet - make a playlist first."
            )

    def _mc_report(self, report, verb: str) -> None:
        if report.get("path") is None:
            messagebox.showinfo(
                "yt2disc",
                "Minecraft's options.txt was not found. Start the game once so it "
                "creates one, then try again.",
            )
            return
        lines = [f"{verb}:", f"  {report['path']}"]
        after = report.get("after") or {}
        for key in (core.MUSIC_OPTION_KEY, core.RECORD_OPTION_KEY):
            if key in after:
                lines.append(f"  {key} = {after[key]}")
        if report.get("backup"):
            lines.append(f"  previous settings kept in {report['backup'].name}")
        if report.get("minecraft_running"):
            lines += [
                "",
                "Minecraft is running: the game rewrites options.txt when it exits,",
                "so close it for this to stick.",
            ]
        self._append_log(lines[0] + " " + str(report["path"]))
        messagebox.showinfo("Minecraft music", "\n".join(lines))

    def _on_mc_off(self) -> None:
        try:
            report = core.mute_minecraft_music(log=self._log)
        except core.YT2DiscError as exc:
            messagebox.showerror("yt2disc", str(exc))
            return
        self._mc_report(report, "Minecraft's background music switched off")

    def _on_mc_on(self) -> None:
        try:
            report = core.unmute_minecraft_music(log=self._log)
        except core.YT2DiscError as exc:
            messagebox.showerror("yt2disc", str(exc))
            return
        self._mc_report(report, "Minecraft's background music switched back on")

    def _on_mc_restore(self) -> None:
        try:
            report = core.restore_minecraft_options(log=self._log)
        except core.YT2DiscError as exc:
            messagebox.showerror("yt2disc", str(exc))
            return
        self._mc_report(report, "Restored options.txt from the backup")

    def _on_clear_cache(self) -> None:
        removed = core.clear_cache(log=self._log)
        self.status_var.set(f"Cache cleared ({removed} file(s)).")

    def _show_about(self) -> None:
        messagebox.showinfo(
            "About yt2disc",
            "\n".join(
                [
                    "yt2disc - a portable Minecraft music player.",
                    "",
                    f"App folder   : {core.SCRIPT_DIR}",
                    "Running from : "
                    + ("frozen executable" if core.is_frozen() else "python sources"),
                    f"Playlists    : {core.PLAYLISTS_PATH}",
                    f"Cache        : {core.CACHE_DIR}",
                    f"ffmpeg       : {core.find_binary('ffmpeg') or 'not found'}",
                    f"Soundtrack   : {core.find_minecraft_content() or 'not found'}",
                    f"options.txt  : {core.find_options_file() or 'not found'}",
                    "",
                    "Muting the game writes audio_music:0 into Minecraft's own",
                    "options.txt and puts the old value back when playback stops.",
                    "",
                    "Playback uses the Windows standard library player: a track plays",
                    "from start to finish, and there is no pause, seek or volume.",
                ]
            ),
        )

    def _on_close(self) -> None:
        try:
            self.engine.stop()
        except Exception:  # pragma: no cover - never block closing
            pass
        self.root.destroy()


def main(argv=None) -> int:  # noqa: ARG001 - kept symmetric with cli.run
    """Start the desktop GUI. Returns a process exit code."""
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        core.ensure_console()
        print(
            f"yt2disc: the GUI could not start ({exc}).\n"
            "Use the command line instead, e.g.  main.py --cli soundtrack --list",
            file=sys.stderr,
        )
        return 1
    try:
        root.call("tk", "scaling", 1.15)
    except tk.TclError:  # pragma: no cover - platform dependent
        pass
    MusicApp(root)
    root.mainloop()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())









