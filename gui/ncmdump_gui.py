#!/usr/bin/env python3
"""NCM Dump GUI - wxPython frontend for ncmdump."""

from __future__ import annotations

import os
import subprocess
import sys
import threading
from pathlib import Path

import wx
import wx.dataview as dv


def _find_ncmdump() -> str:
    repo_root = Path(__file__).resolve().parent.parent
    for rel in ("build/ncmdump", "build/Release/ncmdump.exe", "build/ncmdump.exe"):
        p = repo_root / rel
        if p.is_file():
            return str(p)
    return "ncmdump"


NCMDUMP = _find_ncmdump()


class FileItem:
    def __init__(self, path: Path) -> None:
        self.name = path.name
        self.directory = str(path.parent)
        self.filepath = str(path)
        self.status = "等待转换"


class ConversionThread(threading.Thread):
    def __init__(self, files: list[FileItem], output_dir: str, callback) -> None:
        super().__init__(daemon=True)
        self.files = files
        self.output_dir = output_dir
        self.callback = callback
        self._cancel = False

    def cancel(self) -> None:
        self._cancel = True

    def run(self) -> None:
        for i, item in enumerate(self.files):
            if self._cancel:
                break
            wx.CallAfter(self.callback, i, "转换中...")
            try:
                result = subprocess.run(
                    [NCMDUMP, item.filepath, "-o", self.output_dir],
                    capture_output=True, text=True, timeout=120,
                )
                status = "完成" if result.returncode == 0 else "失败"
            except subprocess.TimeoutExpired:
                status = "超时"
            except Exception:
                status = "错误"
            wx.CallAfter(self.callback, i, status)
        wx.CallAfter(self.callback, -1, "")  # signal done


class NcmDumpFrame(wx.Frame):
    def __init__(self) -> None:
        super().__init__(None, title="NCM Dump", size=(880, 540))
        self.SetMinSize(wx.Size(640, 360))

        self.files: list[FileItem] = []
        self.convert_thread: ConversionThread | None = None
        self.input_dir = ""
        self.output_dir = ""

        self._setup_ui()
        self._setup_statusbar()
        self.Centre()
        self.Show()

    # ── UI setup ──────────────────────────────────────────────

    def _setup_ui(self) -> None:
        panel = wx.Panel(self)
        sizer = wx.BoxSizer(wx.VERTICAL)

        # ── Toolbar row ──
        toolbar = wx.BoxSizer(wx.HORIZONTAL)
        self.btn_input = wx.Button(panel, label="选择输入目录")
        self.btn_output = wx.Button(panel, label="选择输出目录")
        self.btn_convert = wx.Button(panel, label="开始转换")
        self.btn_clear = wx.Button(panel, label="清空列表")

        toolbar.Add(self.btn_input, 0, wx.RIGHT, 6)
        toolbar.Add(self.btn_output, 0, wx.RIGHT, 6)
        toolbar.Add(self.btn_convert, 0, wx.RIGHT, 6)
        toolbar.Add(self.btn_clear, 0)

        sizer.Add(toolbar, 0, wx.ALL | wx.EXPAND, 10)

        # ── Path info ──
        path_sizer = wx.FlexGridSizer(2, 2, 4, 8)
        path_sizer.AddGrowableCol(1)
        self.lbl_input = wx.StaticText(panel, label="未选择")
        self.lbl_output = wx.StaticText(panel, label="未选择")
        path_sizer.Add(wx.StaticText(panel, label="输入:"), 0, wx.ALIGN_CENTRE_VERTICAL)
        path_sizer.Add(self.lbl_input, 0, wx.EXPAND)
        path_sizer.Add(wx.StaticText(panel, label="输出:"), 0, wx.ALIGN_CENTRE_VERTICAL)
        path_sizer.Add(self.lbl_output, 0, wx.EXPAND)
        sizer.Add(path_sizer, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.EXPAND, 10)

        # ── Progress bar ──
        self.gauge = wx.Gauge(panel, range=100)
        sizer.Add(self.gauge, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.EXPAND, 10)

        # ── DataView list ──
        self.dvlc = dv.DataViewListCtrl(panel)
        self.dvlc.AppendTextColumn("文件名", width=220)
        self.dvlc.AppendTextColumn("所在目录", width=420)
        self.dvlc.AppendTextColumn("状态", width=90, align=wx.ALIGN_CENTER)
        sizer.Add(self.dvlc, 1, wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.EXPAND, 10)

        panel.SetSizer(sizer)

        # ── Bind events ──
        self.btn_input.Bind(wx.EVT_BUTTON, self._on_select_input)
        self.btn_output.Bind(wx.EVT_BUTTON, self._on_select_output)
        self.btn_convert.Bind(wx.EVT_BUTTON, self._on_start_convert)
        self.btn_clear.Bind(wx.EVT_BUTTON, self._on_clear)
        self.Bind(wx.EVT_CLOSE, self._on_close)

    def _setup_statusbar(self) -> None:
        self.sb = self.CreateStatusBar()
        self.sb.SetStatusText("就绪")

    # ── Event handlers ────────────────────────────────────────

    def _on_select_input(self, event) -> None:
        dlg = wx.DirDialog(self, "选择包含 .ncm 文件的目录", style=wx.DD_DEFAULT_STYLE)
        if dlg.ShowModal() == wx.ID_OK:
            self.input_dir = dlg.GetPath()
            self.lbl_input.SetLabel(self.input_dir)
            self._scan_files()
        dlg.Destroy()

    def _on_select_output(self, event) -> None:
        dlg = wx.DirDialog(self, "选择输出目录", style=wx.DD_DEFAULT_STYLE)
        if dlg.ShowModal() == wx.ID_OK:
            self.output_dir = dlg.GetPath()
            self.lbl_output.SetLabel(self.output_dir)
        dlg.Destroy()

    def _scan_files(self) -> None:
        self._on_clear(None)
        root = Path(self.input_dir)
        if not root.is_dir():
            return
        self.files = [FileItem(f) for f in sorted(root.rglob("*.ncm"))]
        for item in self.files:
            self.dvlc.AppendItem([item.name, item.directory, item.status])
        self.sb.SetStatusText(f"扫描完成，找到 {len(self.files)} 个 .ncm 文件")

    def _on_clear(self, event) -> None:
        self.dvlc.DeleteAllItems()
        self.files.clear()
        self.gauge.SetValue(0)
        self.sb.SetStatusText("就绪")

    def _on_start_convert(self, event) -> None:
        if self.convert_thread and self.convert_thread.is_alive():
            wx.MessageBox("转换正在进行中", "提示", wx.OK | wx.ICON_INFORMATION)
            return
        if not self.files:
            wx.MessageBox("请先选择输入目录", "提示", wx.OK | wx.ICON_INFORMATION)
            return
        if not self.output_dir:
            wx.MessageBox("请先选择输出目录", "提示", wx.OK | wx.ICON_INFORMATION)
            return
        if not Path(NCMDUMP).exists():
            import shutil
            if not shutil.which(NCMDUMP):
                wx.MessageBox(f"找不到 ncmdump: {NCMDUMP}\n请先运行 make build", "错误", wx.OK | wx.ICON_ERROR)
                return

        self.gauge.SetRange(len(self.files))
        self.gauge.SetValue(0)
        self.btn_convert.Disable()
        self.sb.SetStatusText("转换中...")

        self.convert_thread = ConversionThread(
            self.files, self.output_dir, self._on_convert_update
        )
        self.convert_thread.start()

    def _on_convert_update(self, index: int, status: str) -> None:
        if index < 0:
            # Done signal
            self.btn_convert.Enable()
            self.sb.SetStatusText(f"转换完成，共处理 {len(self.files)} 个文件")
            return
        self.files[index].status = status
        self.dvlc.SetValue(status, index, 2)
        self.gauge.SetValue(index + 1)
        if index == 0:
            self.sb.SetStatusText(f"正在转换: {self.files[0].name}")

    def _on_close(self, event) -> None:
        if self.convert_thread and self.convert_thread.is_alive():
            self.convert_thread.cancel()
        self.Destroy()


def main() -> None:
    app = wx.App()
    NcmDumpFrame()
    app.MainLoop()


if __name__ == "__main__":
    main()
