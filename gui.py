#!/usr/bin/env python3
"""
Tkinter GUI wrapper around orchestrator.py.

Lets you click instead of typing. Runs all commands as subprocesses of
orchestrator.py so the CLI stays the source of truth — the GUI is just a
front-end.

Requirements:
  * Python 3.7+ with tkinter (stdlib; on Linux: ``apt install python3-tk``)
  * orchestrator.py and config.json in the same directory

Launch:
  python3 gui.py                   # uses config.json
  python3 gui.py --config foo.json
"""
import argparse
import json
import queue
import subprocess
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, scrolledtext, ttk


HERE = Path(__file__).resolve().parent
ORCH = HERE / "orchestrator.py"
RESOLUTIONS = ["HD2K", "HD1080", "HD720", "VGA"]
FPSES = ["15", "30", "60", "100"]


class App:
    def __init__(self, root, config_path):
        self.root = root
        self.root.title("ZED Multicam Recorder")
        self.config_path = config_path
        self.config = None
        self.q = queue.Queue()
        self.proc = None
        self._build_ui()
        self._load_config()
        self._poll_queue()

    # ---- UI construction ----------------------------------------------------

    def _build_ui(self):
        # Top: config picker
        top = ttk.Frame(self.root, padding=8)
        top.pack(fill=tk.X)
        ttk.Label(top, text="Config :").pack(side=tk.LEFT)
        self.config_var = tk.StringVar(value=str(self.config_path))
        ttk.Entry(top, textvariable=self.config_var, width=40).pack(side=tk.LEFT, padx=4)
        ttk.Button(top, text="...", command=self._pick_config).pack(side=tk.LEFT)
        ttk.Button(top, text="Reload", command=self._load_config).pack(side=tk.LEFT, padx=4)

        # Hosts table
        cols = ("ip", "label", "user")
        host_frame = ttk.LabelFrame(self.root, text="Fleet", padding=4)
        host_frame.pack(fill=tk.X, padx=8, pady=2)
        self.tree = ttk.Treeview(host_frame, columns=cols, show="headings", height=5)
        for c, w in [("ip", 130), ("label", 160), ("user", 100)]:
            self.tree.heading(c, text=c)
            self.tree.column(c, width=w, anchor=tk.W)
        self.tree.pack(fill=tk.X)

        # Daemon control
        daemon = ttk.LabelFrame(self.root, text="Daemons", padding=8)
        daemon.pack(fill=tk.X, padx=8, pady=4)
        ttk.Label(daemon, text="Resolution :").grid(row=0, column=0, sticky="w")
        self.res_var = tk.StringVar(value="HD1080")
        ttk.Combobox(daemon, textvariable=self.res_var, values=RESOLUTIONS,
                     width=8, state="readonly").grid(row=0, column=1, padx=4)
        ttk.Label(daemon, text="FPS :").grid(row=0, column=2, sticky="w", padx=(12, 0))
        self.fps_var = tk.StringVar(value="30")
        ttk.Combobox(daemon, textvariable=self.fps_var, values=FPSES,
                     width=5, state="readonly").grid(row=0, column=3, padx=4)
        ttk.Button(daemon, text="Launch daemons",
                   command=self._launch).grid(row=0, column=4, padx=8)
        ttk.Button(daemon, text="Restart (redeploy)",
                   command=self._restart).grid(row=0, column=5, padx=2)
        ttk.Button(daemon, text="Kill daemons",
                   command=lambda: self._run("kill")).grid(row=0, column=6, padx=2)
        ttk.Button(daemon, text="Ping",
                   command=lambda: self._run("ping")).grid(row=0, column=7, padx=2)
        ttk.Button(daemon, text="List cams",
                   command=lambda: self._run("list-cams")).grid(row=0, column=8, padx=2)
        ttk.Button(daemon, text="Status",
                   command=lambda: self._run("status")).grid(row=0, column=9, padx=2)

        # Record
        rec = ttk.LabelFrame(self.root, text="Record", padding=8)
        rec.pack(fill=tk.X, padx=8, pady=4)
        ttk.Label(rec, text="Duration (s) :").grid(row=0, column=0, sticky="w")
        self.dur_var = tk.StringVar(value="60")
        ttk.Entry(rec, textvariable=self.dur_var, width=8).grid(row=0, column=1, padx=4)
        ttk.Label(rec, text="Label :").grid(row=0, column=2, sticky="w", padx=(12, 0))
        self.label_var = tk.StringVar(value="test")
        ttk.Entry(rec, textvariable=self.label_var, width=24).grid(row=0, column=3, padx=4)
        ttk.Button(rec, text="Record", command=self._record).grid(row=0, column=4, padx=8)

        # Post-record
        post = ttk.LabelFrame(self.root, text="After recording", padding=8)
        post.pack(fill=tk.X, padx=8, pady=4)
        ttk.Label(post, text="Local SVO dir :").grid(row=0, column=0, sticky="w")
        self.local_dir_var = tk.StringVar(value="./svo")
        ttk.Entry(post, textvariable=self.local_dir_var, width=30).grid(row=0, column=1, padx=4)
        ttk.Button(post, text="...",
                   command=self._pick_local_dir).grid(row=0, column=2)
        ttk.Button(post, text="Convert MP4 (remote)",
                   command=lambda: self._run("convert-mp4")).grid(row=0, column=3, padx=4)
        ttk.Button(post, text="Pull",
                   command=lambda: self._run("pull", "--local-dir", self.local_dir_var.get())
                   ).grid(row=0, column=4, padx=4)
        ttk.Button(post, text="Analyze",
                   command=lambda: self._run("analyze", "--local-dir", self.local_dir_var.get())
                   ).grid(row=0, column=5, padx=4)
        ttk.Button(post, text="Play sync",
                   command=self._play_sync).grid(row=0, column=6, padx=4)
        ttk.Button(post, text="Clean remote",
                   command=self._clean).grid(row=0, column=7, padx=4)

        # Log area
        log_frame = ttk.LabelFrame(self.root, text="Output", padding=4)
        log_frame.pack(fill=tk.BOTH, expand=True, padx=8, pady=4)
        self.log = scrolledtext.ScrolledText(log_frame, height=15,
                                             font=("monospace", 9), wrap=tk.NONE)
        self.log.pack(fill=tk.BOTH, expand=True)
        # Status bar
        self.status_var = tk.StringVar(value="ready")
        ttk.Label(self.root, textvariable=self.status_var,
                  relief=tk.SUNKEN, anchor=tk.W).pack(fill=tk.X, side=tk.BOTTOM)

    # ---- handlers -----------------------------------------------------------

    def _pick_config(self):
        path = filedialog.askopenfilename(
            filetypes=[("JSON", "*.json"), ("All", "*.*")],
            initialdir=HERE)
        if path:
            self.config_var.set(path)
            self._load_config()

    def _pick_local_dir(self):
        path = filedialog.askdirectory(initialdir=self.local_dir_var.get())
        if path:
            self.local_dir_var.set(path)

    def _load_config(self):
        path = Path(self.config_var.get())
        try:
            with open(path) as f:
                cfg = json.load(f)
            self.config = cfg
            self.config_path = path
            self.tree.delete(*self.tree.get_children())
            default_user = cfg.get("default_ssh_user", "zed")
            for h in cfg["hosts"]:
                self.tree.insert("", "end", iid=h["ip"],
                                 values=(h["ip"],
                                         h.get("label", h["ip"]),
                                         h.get("user", default_user)))
            self._log(f"Loaded {path} ({len(cfg['hosts'])} hosts)\n")
            self.status_var.set(f"config: {path.name} — {len(cfg['hosts'])} hosts")
        except Exception as e:
            messagebox.showerror("Config load failed", str(e))

    def _launch(self):
        self._run("launch", "--resolution", self.res_var.get(),
                  "--fps", self.fps_var.get())

    def _restart(self):
        """Equivalent to deploy-recorder + launch — useful after a Jetson reboot
        or /tmp wipe, when ping starts returning ConnectionRefused."""
        self._run("restart", "--resolution", self.res_var.get(),
                  "--fps", self.fps_var.get())

    def _record(self):
        try:
            float(self.dur_var.get())
        except ValueError:
            messagebox.showerror("Bad input",
                                 f"Duration must be a number, got: {self.dur_var.get()!r}")
            return
        label = self.label_var.get().strip() or "test"
        self._run("record", "--duration", self.dur_var.get(), "--label", label)

    def _clean(self):
        if messagebox.askyesno("Confirm",
                               "Delete all remote recordings on every host?"):
            self._run("clean", "--yes")

    def _play_sync(self):
        """Launch playback.py in a separate process so the GUI stays responsive
        while the cv2 window is up."""
        if self.proc and self.proc.poll() is None:
            messagebox.showwarning("Busy", "A command is already running. Wait for it to finish.")
            return
        cmd = [sys.executable, str(HERE / "playback.py"), self.local_dir_var.get()]
        self._log(f"\n$ {' '.join(cmd)}\n")
        self.status_var.set("running: playback")
        threading.Thread(target=self._run_thread, args=(cmd,), daemon=True).start()

    def _run(self, *subcmd):
        if self.proc and self.proc.poll() is None:
            messagebox.showwarning("Busy",
                                   "A command is already running. Wait for it to finish.")
            return
        cmd = [sys.executable, str(ORCH), "--config", str(self.config_path),
               *subcmd]
        self._log(f"\n$ {' '.join(cmd)}\n")
        self.status_var.set(f"running: {subcmd[0]}")
        threading.Thread(target=self._run_thread, args=(cmd,), daemon=True).start()

    def _run_thread(self, cmd):
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT,
                                    text=True, bufsize=1, cwd=str(HERE))
            self.proc = proc
            for line in proc.stdout:
                self.q.put(line)
            proc.wait()
            self.q.put(f"[exit {proc.returncode}]\n")
            self.q.put(("__status__", "ready"))
        except Exception as e:
            self.q.put(f"\n[error] {e}\n")
            self.q.put(("__status__", "error"))

    def _log(self, text):
        self.log.insert(tk.END, text)
        self.log.see(tk.END)

    def _poll_queue(self):
        try:
            while True:
                item = self.q.get_nowait()
                if isinstance(item, tuple) and item[0] == "__status__":
                    self.status_var.set(item[1])
                else:
                    self._log(item)
        except queue.Empty:
            pass
        self.root.after(120, self._poll_queue)


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--config", default=str(HERE / "config.json"),
                   help="Path to fleet config (default: ./config.json)")
    args = p.parse_args()

    root = tk.Tk()
    root.geometry("960x720")
    App(root, Path(args.config))
    root.mainloop()


if __name__ == "__main__":
    main()
