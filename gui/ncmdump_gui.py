#!/usr/bin/env python3
"""NCM Dump GUI – wxPython frontend with metadata & MP3 conversion support."""

from __future__ import annotations

import os
import shutil
import subprocess
import threading
from pathlib import Path

import wx
import wx.dataview as dv

from logger import log
from settings import load as load_settings, save as save_settings
from metadata import (
    extract_ncm_meta,
    parse_path_meta,
    find_cover,
    build_ffmpeg_meta_cmd,
)


def _find_ncmdump() -> str:
    repo = Path(__file__).resolve().parent.parent
    for rel in ("build/ncmdump", "build/Release/ncmdump.exe", "build/ncmdump.exe"):
        p = repo / rel
        if p.is_file():
            return str(p)
    return "ncmdump"


def _find_ffmpeg() -> str:
    p = shutil.which("ffmpeg")
    return p or "ffmpeg"


NCMDUMP = _find_ncmdump()
FFMPEG = _find_ffmpeg()


# ── Data model ────────────────────────────────────────────────

class FileItem:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.name = path.name
        self.directory = str(path.parent)
        self.status = "等待转换"

        self.music_id: int | None = None
        self.ncm_artist = ""
        self.ncm_album = ""

        # Derived from directory structure
        self.dir_artist, self.dir_album, self.dir_title = parse_path_meta(path)


# ── Conversion thread ─────────────────────────────────────────

class ConvertThread(threading.Thread):
    def __init__(
        self,
        items: list[FileItem],
        output_dir: str,
        mp3_convert: bool,
        fill_meta: bool,
        music_root: str,
        callback,
    ) -> None:
        super().__init__(daemon=True)
        self.items = items
        self.output_dir = output_dir
        self.mp3_convert = mp3_convert
        self.fill_meta = fill_meta
        self.music_root = music_root
        self.callback = callback
        self._cancel = False

    def cancel(self) -> None:
        self._cancel = True

    def run(self) -> None:
        for i, item in enumerate(self.items):
            if self._cancel:
                break
            self._update(i, "解密中...")
            try:
                self._process_one(item)
            except Exception as e:
                log.error("[convert] %s: unexpected error: %s", item.name, e)
                self._update(i, "错误")
            else:
                self._update(i, "完成")
        self._update(-1, "")

    def _update(self, index: int, status: str) -> None:
        wx.CallAfter(self.callback, index, status)

    def _process_one(self, item: FileItem) -> None:
        src = str(item.path)
        tmp_out = os.path.join(self.output_dir, item.path.stem)
        final_out = tmp_out

        # Step 1: decrypt via ncmdump
        log.info("[decrypt] %s", item.name)
        log.debug("[decrypt] cmd: %s \"%s\" -o \"%s\"", NCMDUMP, src, self.output_dir)
        r = subprocess.run(
            [NCMDUMP, src, "-o", self.output_dir],
            capture_output=True, text=True, timeout=120,
        )
        log.debug("[decrypt] stdout: %s", r.stdout.strip())
        if r.returncode != 0:
            log.error("[decrypt] %s FAILED (rc=%d): %s", item.name, r.returncode, r.stderr.strip())
            self._update(self.items.index(item), "失败")
            return

        # ncmdump outputs: output_dir/song.mp3 or output_dir/song.flac
        for ext in (".mp3", ".flac"):
            candidate = tmp_out + ext
            if os.path.isfile(candidate):
                final_out = candidate
                break

        if final_out == tmp_out:
            log.error("[decrypt] %s: output file not found", item.name)
            self._update(self.items.index(item), "失败")
            return

        # Step 2: extract ncm metadata (musicId etc.) if needed
        if self.fill_meta or self.mp3_convert:
            meta = extract_ncm_meta(src)
            if meta:
                item.music_id = meta.get("musicId")
                item.ncm_artist = str(meta.get("artist", [[""]])[0][0]) if isinstance(meta.get("artist"), list) else ""
                item.ncm_album = str(meta.get("album", ""))

        # Decide artist/album/title
        artist = item.dir_artist or item.ncm_artist or ""
        album = item.dir_album or item.ncm_album or ""
        title = item.dir_title or item.name.replace(".ncm", "")

        # Find cover
        cover = None
        if self.fill_meta and item.music_id:
            cover = find_cover(item.music_id, self.music_root)
            if cover:
                log.info("[cover] %s -> %s", item.name, os.path.basename(cover))

        # Step 3: ffmpeg processing
        if self.mp3_convert:
            self._update(self.items.index(item), "转码 MP3...")
            mp3_out = tmp_out + ".mp3"
            cmd = build_ffmpeg_meta_cmd(
                final_out, mp3_out,
                artist=artist if self.fill_meta else "",
                album=album if self.fill_meta else "",
                title=title if self.fill_meta else "",
                cover=cover,
                mp3_bitrate="320k",
            )
            log.info("[ffmpeg] %s -> mp3", item.name)
            log.debug("[ffmpeg] cmd: %s", " ".join(cmd))
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
            if r.returncode != 0:
                log.error("[ffmpeg] %s FAILED: %s", item.name, r.stderr.strip()[-200:])
                self._update(self.items.index(item), "失败")
                return
            # Remove intermediate file (original decrypted flac/mp3)
            if os.path.isfile(final_out) and final_out != mp3_out:
                os.remove(final_out)
                log.debug("[ffmpeg] removed intermediate: %s", final_out)

        elif self.fill_meta:
            # Apply metadata to existing file (re-encode to same format with tags)
            self._update(self.items.index(item), "写入元数据...")
            ext = os.path.splitext(final_out)[1]
            tagged_out = tmp_out + ".tagged" + ext
            cmd = build_ffmpeg_meta_cmd(
                final_out, tagged_out,
                artist=artist, album=album, title=title, cover=cover,
                mp3_bitrate="320k",
            )
            log.info("[tag] %s", item.name)
            log.debug("[tag] cmd: %s", " ".join(cmd))
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
            if r.returncode == 0:
                os.replace(tagged_out, final_out)
            else:
                log.error("[tag] %s FAILED: %s", item.name, r.stderr.strip()[-200:])


# ── Main frame ────────────────────────────────────────────────

class NcmDumpFrame(wx.Frame):
    def __init__(self) -> None:
        super().__init__(None, title="NCM Dump", size=(940, 600))
        self.SetMinSize(wx.Size(680, 400))

        self.files: list[FileItem] = []
        self.convert_thread: ConvertThread | None = None
        self.input_dir = ""
        self.output_dir = ""
        self.music_root = ""

        self.settings = load_settings()

        self._setup_ui()
        self._setup_statusbar()
        self.Centre()
        self.Show()

    # ── UI ────────────────────────────────────────────────────

    def _setup_ui(self) -> None:
        panel = wx.Panel(self)
        sizer = wx.BoxSizer(wx.VERTICAL)

        # ── Toolbar ──
        tb = wx.BoxSizer(wx.HORIZONTAL)
        self.btn_input = wx.Button(panel, label="选择输入目录")
        self.btn_output = wx.Button(panel, label="选择输出目录")
        self.btn_go = wx.Button(panel, label="开始转换")
        self.btn_clear = wx.Button(panel, label="清空列表")
        tb.Add(self.btn_input, 0, wx.RIGHT, 6)
        tb.Add(self.btn_output, 0, wx.RIGHT, 6)
        tb.Add(self.btn_go, 0, wx.RIGHT, 6)
        tb.Add(self.btn_clear, 0)
        sizer.Add(tb, 0, wx.ALL | wx.EXPAND, 10)

        # ── Path info ──
        grid = wx.FlexGridSizer(2, 2, 4, 8)
        grid.AddGrowableCol(1)
        self.lbl_input = wx.StaticText(panel, label="未选择")
        self.lbl_output = wx.StaticText(panel, label="未选择")
        grid.Add(wx.StaticText(panel, label="输入:"), 0, wx.ALIGN_CENTRE_VERTICAL)
        grid.Add(self.lbl_input, 0, wx.EXPAND)
        grid.Add(wx.StaticText(panel, label="输出:"), 0, wx.ALIGN_CENTRE_VERTICAL)
        grid.Add(self.lbl_output, 0, wx.EXPAND)
        sizer.Add(grid, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.EXPAND, 10)

        # ── Advanced options ──
        adv_box = wx.StaticBox(panel, label="高级选项")
        adv_sizer = wx.StaticBoxSizer(adv_box, wx.HORIZONTAL)

        self.cb_mp3 = wx.CheckBox(panel, label="转换为 MP3 (iPod 兼容, 需 ffmpeg)")
        self.cb_mp3.SetValue(self.settings.get("mp3_convert", False))
        self.cb_fill = wx.CheckBox(panel, label="从目录结构填充元数据")
        self.cb_fill.SetValue(self.settings.get("fill_metadata", False))

        adv_sizer.Add(self.cb_mp3, 0, wx.ALL | wx.ALIGN_CENTRE_VERTICAL, 6)
        adv_sizer.Add(self.cb_fill, 0, wx.ALL | wx.ALIGN_CENTRE_VERTICAL, 6)
        adv_sizer.AddStretchSpacer()

        # Music root path (for cover lookup)
        self.lbl_mroot = wx.StaticText(panel, label="音乐根目录: (自动检测)")
        btn_mroot = wx.Button(panel, label="设置根目录", size=(100, -1))
        adv_sizer.Add(self.lbl_mroot, 0, wx.ALL | wx.ALIGN_CENTRE_VERTICAL, 6)
        adv_sizer.Add(btn_mroot, 0, wx.ALL | wx.ALIGN_CENTRE_VERTICAL, 6)

        sizer.Add(adv_sizer, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.EXPAND, 10)

        # ── Progress ──
        self.gauge = wx.Gauge(panel, range=100)
        sizer.Add(self.gauge, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.EXPAND, 10)

        # ── List ──
        self.dvlc = dv.DataViewListCtrl(panel)
        self.dvlc.AppendTextColumn("文件名", width=220)
        self.dvlc.AppendTextColumn("所在目录", width=420)
        self.dvlc.AppendTextColumn("状态", width=90, align=wx.ALIGN_CENTER)
        sizer.Add(self.dvlc, 1, wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.EXPAND, 10)

        panel.SetSizer(sizer)

        # ── Events ──
        self.btn_input.Bind(wx.EVT_BUTTON, self._on_input)
        self.btn_output.Bind(wx.EVT_BUTTON, self._on_output)
        self.btn_go.Bind(wx.EVT_BUTTON, self._on_go)
        self.btn_clear.Bind(wx.EVT_BUTTON, self._on_clear)
        self.cb_mp3.Bind(wx.EVT_CHECKBOX, self._on_setting_changed)
        self.cb_fill.Bind(wx.EVT_CHECKBOX, self._on_setting_changed)
        btn_mroot.Bind(wx.EVT_BUTTON, self._on_set_mroot)
        self.Bind(wx.EVT_CLOSE, self._on_close)

    def _setup_statusbar(self) -> None:
        self.sb = self.CreateStatusBar()
        self.sb.SetStatusText("就绪")

    # ── Event handlers ────────────────────────────────────────

    def _on_input(self, _event) -> None:
        dlg = wx.DirDialog(self, "选择包含 .ncm 文件的目录")
        if dlg.ShowModal() == wx.ID_OK:
            self.input_dir = dlg.GetPath()
            self.lbl_input.SetLabel(self.input_dir)
            self._auto_detect_music_root()
            self._scan()
        dlg.Destroy()

    def _on_output(self, _event) -> None:
        dlg = wx.DirDialog(self, "选择输出目录")
        if dlg.ShowModal() == wx.ID_OK:
            self.output_dir = dlg.GetPath()
            self.lbl_output.SetLabel(self.output_dir)
        dlg.Destroy()

    def _on_set_mroot(self, _event) -> None:
        dlg = wx.DirDialog(self, "选择音乐根目录（包含 meta 文件夹）")
        if dlg.ShowModal() == wx.ID_OK:
            self.music_root = dlg.GetPath()
            self.lbl_mroot.SetLabel(f"音乐根目录: {self.music_root}")
        dlg.Destroy()

    def _auto_detect_music_root(self) -> None:
        """Try to find the music root by walking up from input_dir."""
        p = Path(self.input_dir)
        for _ in range(5):
            if (p / "meta").is_dir():
                self.music_root = str(p)
                self.lbl_mroot.SetLabel(f"音乐根目录: {self.music_root}")
                return
            p = p.parent
        self.music_root = ""
        self.lbl_mroot.SetLabel("音乐根目录: (未检测到)")

    def _on_setting_changed(self, _event) -> None:
        self.settings["mp3_convert"] = self.cb_mp3.GetValue()
        self.settings["fill_metadata"] = self.cb_fill.GetValue()
        save_settings(self.settings)

    def _scan(self) -> None:
        self._on_clear(None)
        root = Path(self.input_dir)
        if not root.is_dir():
            return
        ncm_paths = sorted(root.rglob("*.ncm"))
        self.files = [FileItem(p) for p in ncm_paths]
        for item in self.files:
            self.dvlc.AppendItem([item.name, item.directory, item.status])
        cnt = len(self.files)
        log.info("[scan] %d .ncm files found in %s", cnt, self.input_dir)
        self.sb.SetStatusText(f"扫描完成，找到 {cnt} 个文件")

    def _on_clear(self, _event) -> None:
        self.dvlc.DeleteAllItems()
        self.files.clear()
        self.gauge.SetValue(0)
        self.sb.SetStatusText("就绪")
        log.info("[list] cleared")

    def _on_go(self, _event) -> None:
        if self.convert_thread and self.convert_thread.is_alive():
            wx.MessageBox("转换正在进行中", "提示", wx.OK | wx.ICON_INFORMATION)
            return
        if not self.files:
            wx.MessageBox("请先选择输入目录", "提示", wx.OK | wx.ICON_INFORMATION)
            return
        if not self.output_dir:
            wx.MessageBox("请先选择输出目录", "提示", wx.OK | wx.ICON_INFORMATION)
            return

        mp3 = self.cb_mp3.GetValue()
        fill = self.cb_fill.GetValue()

        if mp3 and not shutil.which("ffmpeg"):
            wx.MessageBox("需要 ffmpeg 但未找到。请运行 make setup 安装。", "错误", wx.OK | wx.ICON_ERROR)
            return

        log.info("=== 开始转换 ===")
        log.info("  输入: %s", self.input_dir)
        log.info("  输出: %s", self.output_dir)
        log.info("  MP3: %s  元数据: %s", mp3, fill)

        self.gauge.SetRange(len(self.files))
        self.gauge.SetValue(0)
        self.btn_go.Disable()
        self.sb.SetStatusText("转换中...")

        self.convert_thread = ConvertThread(
            self.files, self.output_dir, mp3, fill,
            self.music_root, self._on_progress,
        )
        self.convert_thread.start()

    def _on_progress(self, index: int, status: str) -> None:
        if index < 0:
            self.btn_go.Enable()
            self.sb.SetStatusText(f"转换完成，共处理 {len(self.files)} 个文件")
            log.info("=== 转换完成 ===")
            return
        self.files[index].status = status
        self.dvlc.SetValue(status, index, 2)
        self.gauge.SetValue(index + 1)

    def _on_close(self, _event) -> None:
        if self.convert_thread and self.convert_thread.is_alive():
            self.convert_thread.cancel()
        self.Destroy()


def main() -> None:
    app = wx.App()
    NcmDumpFrame()
    app.MainLoop()


if __name__ == "__main__":
    main()
