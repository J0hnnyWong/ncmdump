#!/usr/bin/env python3
"""NCM Dump GUI - cross-platform tkinter frontend for ncmdump."""
from __future__ import annotations

import os
import subprocess
import sys
import threading
from pathlib import Path
import tkinter as tk
from tkinter import ttk, filedialog, messagebox


class NcmDumpGUI:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("NCM Dump")
        self.root.geometry("860x520")
        self.root.minsize(640, 360)

        self.input_dir = tk.StringVar()
        self.output_dir = tk.StringVar()
        self.running = False

        self._setup_ui()

    # ── UI setup ──────────────────────────────────────────────

    def _setup_ui(self) -> None:
        # Button bar
        btn_frame = ttk.Frame(self.root, padding="10 10 10 5")
        btn_frame.pack(fill=tk.X)

        ttk.Button(btn_frame, text="选择输入目录", command=self._select_input).pack(
            side=tk.LEFT, padx=(0, 6)
        )
        ttk.Button(btn_frame, text="选择输出目录", command=self._select_output).pack(
            side=tk.LEFT, padx=(0, 6)
        )
        ttk.Button(btn_frame, text="开始转换", command=self._start_convert).pack(
            side=tk.LEFT, padx=(0, 6)
        )
        ttk.Button(btn_frame, text="清空列表", command=self._clear_list).pack(
            side=tk.LEFT
        )

        # Path info
        path_frame = ttk.Frame(self.root, padding="10 0 10 5")
        path_frame.pack(fill=tk.X)
        ttk.Label(path_frame, text="输入:", width=5).grid(
            row=0, column=0, sticky=tk.W
        )
        ttk.Label(
            path_frame, textvariable=self.input_dir, foreground="gray"
        ).grid(row=0, column=1, sticky=tk.W, padx=(4, 0))
        ttk.Label(path_frame, text="输出:", width=5).grid(
            row=1, column=0, sticky=tk.W
        )
        ttk.Label(
            path_frame, textvariable=self.output_dir, foreground="gray"
        ).grid(row=1, column=1, sticky=tk.W, padx=(4, 0))

        # File list
        tree_frame = ttk.Frame(self.root, padding="10 5")
        tree_frame.pack(fill=tk.BOTH, expand=True)

        columns = ("filename", "directory", "status")
        self.tree = ttk.Treeview(tree_frame, columns=columns, show="headings")
        self.tree.heading("filename", text="文件名")
        self.tree.heading("directory", text="所在目录")
        self.tree.heading("status", text="状态")
        self.tree.column("filename", width=220, minwidth=120)
        self.tree.column("directory", width=400, minwidth=150)
        self.tree.column("status", width=100, minwidth=70, anchor=tk.CENTER)

        scrollbar = ttk.Scrollbar(
            tree_frame, orient=tk.VERTICAL, command=self.tree.yview
        )
        self.tree.configure(yscrollcommand=scrollbar.set)
        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        # Progress bar
        self.progress = ttk.Progressbar(self.root, mode="determinate")
        self.progress.pack(fill=tk.X, padx=10, pady=(0, 2))

        # Status bar
        self.status_label = ttk.Label(
            self.root, text="就绪", relief=tk.SUNKEN, anchor=tk.W, padding="3 2"
        )
        self.status_label.pack(fill=tk.X, side=tk.BOTTOM)

    # ── Callbacks ─────────────────────────────────────────────

    def _select_input(self) -> None:
        path = filedialog.askdirectory(title="选择包含 .ncm 文件的目录")
        if path:
            self.input_dir.set(path)
            self._scan_files()

    def _select_output(self) -> None:
        path = filedialog.askdirectory(title="选择输出目录")
        if path:
            self.output_dir.set(path)

    def _scan_files(self) -> None:
        self._clear_list()
        root_path = Path(self.input_dir.get())
        if not root_path.is_dir():
            return

        ncm_files = sorted(root_path.rglob("*.ncm"))
        for f in ncm_files:
            self.tree.insert(
                "", tk.END, values=(f.name, str(f.parent), "等待转换")
            )

        self._set_status(f"扫描完成，找到 {len(ncm_files)} 个 .ncm 文件")

    def _clear_list(self) -> None:
        for item in self.tree.get_children():
            self.tree.delete(item)
        self.progress["value"] = 0
        self._set_status("就绪")

    def _start_convert(self) -> None:
        if self.running:
            messagebox.showwarning("提示", "转换正在进行中，请等待完成")
            return

        items = self.tree.get_children()
        if not items:
            messagebox.showwarning("提示", "列表为空，请先选择输入目录")
            return

        output_dir = self.output_dir.get()
        if not output_dir:
            messagebox.showwarning("提示", "请先选择输出目录")
            return

        ncmdump = _find_ncmdump()
        if ncmdump is None:
            messagebox.showerror(
                "错误",
                "找不到 ncmdump 可执行文件。\n请先运行 make build 编译项目。",
            )
            return

        self.running = True
        self.progress["maximum"] = len(items)
        self.progress["value"] = 0

        threading.Thread(
            target=self._run_conversion,
            args=(items, output_dir, ncmdump),
            daemon=True,
        ).start()

    # ── Conversion worker (background thread) ─────────────────

    def _run_conversion(
        self, items: list[str], output_dir: str, ncmdump: str
    ) -> None:
        total = len(items)
        for i, item_id in enumerate(items):
            values = self.tree.item(item_id, "values")
            filename, directory, _ = values
            filepath = os.path.join(directory, filename)

            self._set_item_status(item_id, "转换中...")
            self._set_status(f"正在转换: {filename}")

            try:
                result = subprocess.run(
                    [ncmdump, filepath, "-o", output_dir],
                    capture_output=True,
                    text=True,
                    timeout=120,
                )
                if result.returncode == 0:
                    self._set_item_status(item_id, "完成")
                else:
                    self._set_item_status(item_id, "失败")
            except subprocess.TimeoutExpired:
                self._set_item_status(item_id, "超时")
            except Exception:
                self._set_item_status(item_id, "错误")

            # Increment progress on the main thread
            self.root.after(0, lambda v=i + 1: self.progress.configure(value=v))

        self.running = False
        self._set_status(f"转换完成，共处理 {total} 个文件")

    # ── Thread-safe UI helpers ────────────────────────────────

    def _set_item_status(self, item_id: str, status: str) -> None:
        def _apply() -> None:
            self.tree.set(item_id, "status", status)
        self.root.after(0, _apply)

    def _set_status(self, text: str) -> None:
        def _apply() -> None:
            self.status_label.config(text=text)
        self.root.after(0, _apply)


# ── Helpers ───────────────────────────────────────────────────

def _find_ncmdump() -> str | None:
    """Locate the ncmdump binary relative to the repo root."""
    repo_root = Path(__file__).resolve().parent.parent
    candidates = [
        repo_root / "build" / "ncmdump",
        repo_root / "build" / "Release" / "ncmdump.exe",
        repo_root / "build" / "ncmdump.exe",
    ]
    for c in candidates:
        if c.is_file():
            return str(c)

    # Fallback: try PATH
    for name in ("ncmdump", "ncmdump.exe"):
        if _which(name):
            return name
    return None


def _which(name: str) -> str | None:
    """shutil.which equivalent for Python 3.2+."""
    import shutil
    return shutil.which(name)


# ── Entry point ───────────────────────────────────────────────

def main() -> None:
    root = tk.Tk()
    app = NcmDumpGUI(root)
    root.mainloop()


if __name__ == "__main__":
    main()
