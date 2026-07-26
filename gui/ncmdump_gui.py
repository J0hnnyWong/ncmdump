#!/usr/bin/env python3
"""NCM Dump GUI - tkinter frontend for ncmdump."""

from __future__ import annotations

import os
import subprocess
import sys
import threading
from pathlib import Path
import tkinter as tk
from tkinter import ttk, filedialog, messagebox


def _find_ncmdump() -> str:
    repo_root = Path(__file__).resolve().parent.parent
    candidates = [
        repo_root / "build" / "ncmdump",
        repo_root / "build" / "Release" / "ncmdump.exe",
        repo_root / "build" / "ncmdump.exe",
    ]
    for c in candidates:
        if c.is_file():
            return str(c)
    return "ncmdump"


NCMDUMP = _find_ncmdump()


class NcmDumpGUI:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("NCM Dump")
        self.root.geometry("860x520")
        self.root.minsize(640, 360)

        self.input_dir = tk.StringVar()
        self.output_dir = tk.StringVar()
        self.running = False
        self.convert_thread: threading.Thread | None = None

        self._setup_ui()
        self._setup_styles()

    # ── Styles ────────────────────────────────────────────────

    def _setup_styles(self) -> None:
        style = ttk.Style()
        if "aqua" in style.theme_use():
            style.theme_use("clam")
        style.configure("TButton", padding=6, font=("", 11))
        style.configure("TLabel", font=("", 11))
        style.configure("Treeview", font=("", 11), rowheight=24)
        style.configure("Treeview.Heading", font=("", 11, "bold"))
        style.configure("Red.TButton", foreground="#e94560")
        style.configure("Green.TButton", foreground="#4ecca3")

    # ── UI ────────────────────────────────────────────────────

    def _setup_ui(self) -> None:
        # Top toolbar
        toolbar = ttk.Frame(self.root, padding="10 10 10 6")
        toolbar.pack(fill=tk.X)

        ttk.Button(toolbar, text="选择输入目录", command=self._select_input).pack(
            side=tk.LEFT, padx=(0, 6)
        )
        ttk.Button(toolbar, text="选择输出目录", command=self._select_output).pack(
            side=tk.LEFT, padx=(0, 6)
        )
        ttk.Button(
            toolbar, text="开始转换", command=self._start_convert, style="Red.TButton"
        ).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(toolbar, text="清空列表", command=self._clear_list).pack(
            side=tk.LEFT
        )

        # Path display
        path_frame = ttk.Frame(self.root, padding="10 0 10 6")
        path_frame.pack(fill=tk.X)
        ttk.Label(path_frame, text="输入:", width=5).grid(
            row=0, column=0, sticky=tk.W
        )
        ttk.Label(
            path_frame, textvariable=self.input_dir, foreground="#888"
        ).grid(row=0, column=1, sticky=tk.W, padx=(4, 0))
        ttk.Label(path_frame, text="输出:", width=5).grid(
            row=1, column=0, sticky=tk.W
        )
        ttk.Label(
            path_frame, textvariable=self.output_dir, foreground="#888"
        ).grid(row=1, column=1, sticky=tk.W, padx=(4, 0))

        # Progress bar
        self.progress = ttk.Progressbar(self.root, mode="determinate")
        self.progress.pack(fill=tk.X, padx=10, pady=(4, 1))

        # File list tree
        tree_frame = ttk.Frame(self.root, padding="10 4")
        tree_frame.pack(fill=tk.BOTH, expand=True)

        columns = ("filename", "directory", "status")
        self.tree = ttk.Treeview(tree_frame, columns=columns, show="headings")
        self.tree.heading("filename", text="文件名")
        self.tree.heading("directory", text="所在目录")
        self.tree.heading("status", text="状态")
        self.tree.column("filename", width=220, minwidth=120)
        self.tree.column("directory", width=420, minwidth=150)
        self.tree.column("status", width=90, minwidth=70, anchor=tk.CENTER)

        scrollbar = ttk.Scrollbar(
            tree_frame, orient=tk.VERTICAL, command=self.tree.yview
        )
        self.tree.configure(yscrollcommand=scrollbar.set)
        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        # Status bar
        self.status_var = tk.StringVar(value="就绪")
        status_bar = ttk.Label(
            self.root, textvariable=self.status_var,
            relief=tk.SUNKEN, anchor=tk.W, padding="3 2",
        )
        status_bar.pack(fill=tk.X, side=tk.BOTTOM)

    # ── Actions ───────────────────────────────────────────────

    def _select_input(self) -> None:
        path = filedialog.askdirectory(title="选择包含 .ncm 文件的目录")
        if path:
            self.input_dir.set(path)
            self._scan_files(path)

    def _select_output(self) -> None:
        path = filedialog.askdirectory(title="选择输出目录")
        if path:
            self.output_dir.set(path)

    def _scan_files(self, directory: str) -> None:
        self._clear_list()
        root_path = Path(directory)
        if not root_path.is_dir():
            return
        ncm_files = sorted(root_path.rglob("*.ncm"))
        for f in ncm_files:
            self.tree.insert("", tk.END, values=(f.name, str(f.parent), "等待转换"))
        self._set_status(f"扫描完成，找到 {len(ncm_files)} 个 .ncm 文件")

    def _clear_list(self) -> None:
        for item in self.tree.get_children():
            self.tree.delete(item)
        self.progress["value"] = 0
        self._set_status("就绪")

    def _start_convert(self) -> None:
        if self.running:
            messagebox.showwarning("提示", "转换正在进行中")
            return

        items = self.tree.get_children()
        if not items:
            messagebox.showwarning("提示", "请先选择输入目录")
            return

        output_dir = self.output_dir.get()
        if not output_dir:
            messagebox.showwarning("提示", "请先选择输出目录")
            return

        if not Path(NCMDUMP).exists() and not _which(NCMDUMP):
            messagebox.showerror("错误", f"找不到 ncmdump: {NCMDUMP}\n请先运行 make build")
            return

        self.running = True
        self.progress["maximum"] = len(items)
        self.progress["value"] = 0
        self._set_status("转换中...")

        self.convert_thread = threading.Thread(
            target=self._run_conversion,
            args=(items, output_dir),
            daemon=True,
        )
        self.convert_thread.start()

    # ── Conversion worker ─────────────────────────────────────

    def _run_conversion(self, items: list[str], output_dir: str) -> None:
        total = len(items)
        completed = 0
        for item_id in items:
            values = self.tree.item(item_id, "values")
            filename, directory, _ = values
            filepath = os.path.join(directory, filename)

            self._set_item_status(item_id, "转换中...")
            self._set_status(f"正在转换: {filename}")

            try:
                result = subprocess.run(
                    [NCMDUMP, filepath, "-o", output_dir],
                    capture_output=True, text=True, timeout=120,
                )
                status = "完成" if result.returncode == 0 else "失败"
            except subprocess.TimeoutExpired:
                status = "超时"
            except Exception:
                status = "错误"

            self._set_item_status(item_id, status)
            completed += 1
            self.root.after(0, lambda v=completed: self.progress.configure(value=v))

        self.running = False
        self._set_status(f"转换完成，共处理 {total} 个文件")

    # ── Thread-safe UI helpers ────────────────────────────────

    def _set_item_status(self, item_id: str, status: str) -> None:
        def _apply() -> None:
            self.tree.set(item_id, "status", status)
            # Color-code the status cell
            for child in self.tree.get_children():
                if child == item_id:
                    s = self.tree.set(child, "status")
                    tag = ""
                    if s == "完成":
                        tag = "done"
                    elif s in ("失败", "超时", "错误"):
                        tag = "fail"
                    elif s == "转换中...":
                        tag = "progress"
                    self.tree.item(child, tags=(tag,))
        self.root.after(0, _apply)

    def _set_status(self, text: str) -> None:
        self.root.after(0, lambda: self.status_var.set(text))


def _which(name: str) -> str | None:
    import shutil
    return shutil.which(name)


def main() -> None:
    root = tk.Tk()
    app = NcmDumpGUI(root)

    # Status tag colors
    app.tree.tag_configure("done", foreground="#4ecca3")
    app.tree.tag_configure("fail", foreground="#e94560")
    app.tree.tag_configure("progress", foreground="#f0a500")

    root.mainloop()


if __name__ == "__main__":
    main()
