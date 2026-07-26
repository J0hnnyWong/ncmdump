#!/usr/bin/env python3
"""NCM Dump GUI – wxPython frontend with metadata & MP3 conversion support."""

from __future__ import annotations

import os
import shutil
import subprocess
import threading, traceback
from pathlib import Path

import wx
import wx.dataview as dv

from logger import log
from settings import load as load_settings, save as save_settings
from metadata import (
    extract_ncm_meta,
    parse_path_meta,
    find_cover,
    write_tags,
    read_meta_from_file,
    write_meta_from_snapshot,
    SavedMeta,
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
        self.dir_artist, self.dir_album, self.dir_title = parse_path_meta(path)
        self.ncm_artist = ""
        self.ncm_album = ""


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
            try:
                self._process_one(item, i)
            except Exception as e:
                log.error("[convert] %s: %s\n%s", item.name, e, traceback.format_exc())
                self._update(i, "错误")
        self._update(-1, "")

    def _update(self, index: int, status: str) -> None:
        wx.CallAfter(self.callback, index, status)

    # ── Per-file pipeline ─────────────────────────────────────

    def _process_one(self, item: FileItem, index: int) -> None:
        src = str(item.path)

        # ═══ Phase 1: Decrypt ═══
        self._update(index, "解密中...")
        decrypted = self._decrypt(item, src)
        if decrypted is None:
            return  # status already set to "失败" inside _decrypt

        # ═══ Phase 2: Write metadata to decrypted file ═══
        if self.fill_meta:
            self._update(index, "写入元数据...")
            self._fill_metadata(item, src, decrypted)

        # ═══ Phase 3: Convert to MP3 if requested ═══
        if self.mp3_convert:
            self._update(index, "转码 MP3...")
            self._convert_to_mp3(item, decrypted)
        else:
            self._update(index, "完成")

    def _decrypt(self, item: FileItem, src: str) -> str | None:
        log.info("[decrypt] %s", item.name)
        log.debug("[decrypt] ncmdump \"%s\" -o \"%s\"", src, self.output_dir)
        r = subprocess.run(
            [NCMDUMP, src, "-o", self.output_dir],
            capture_output=True, text=True, timeout=120,
        )
        log.debug("[decrypt] %s", r.stdout.strip())
        if r.returncode != 0:
            log.error("[decrypt] FAILED (rc=%d): %s", r.returncode, r.stderr.strip()[-200:])
            self._update(self.items.index(item), "失败")
            return None

        # Find the output file ncmdump created
        base = os.path.join(self.output_dir, item.path.stem)
        for ext in (".mp3", ".flac"):
            if os.path.isfile(base + ext):
                log.info("[decrypt] -> %s%s", item.path.stem, ext)
                return base + ext

        log.error("[decrypt] output not found for %s", item.name)
        self._update(self.items.index(item), "失败")
        return None

    def _fill_metadata(self, item: FileItem, src: str, decrypted: str) -> None:
        # Determine artist/album/title
        artist = item.dir_artist
        album = item.dir_album
        title = item.dir_title

        # Supplement from ncm JSON if path metadata is incomplete
        if not artist or not album:
            meta = extract_ncm_meta(src)
            if meta:
                item.music_id = meta.get("musicId")
                if not artist and isinstance(meta.get("artist"), list):
                    try:
                        item.ncm_artist = str(meta["artist"][0][0])
                    except (IndexError, TypeError):
                        pass
                if not album:
                    item.ncm_album = str(meta.get("album", ""))
                if not artist:
                    artist = item.ncm_artist
                if not album:
                    album = item.ncm_album

        # Find cover
        if not item.music_id:
            meta = extract_ncm_meta(src)
            if meta:
                item.music_id = meta.get("musicId")

        cover = find_cover(item.music_id, self.music_root) if item.music_id else None

        log.info("[meta] %s  artist=%r album=%r cover=%s",
                 item.name, artist, album,
                 os.path.basename(cover) if cover else "none")

        write_tags(decrypted, artist=artist, album=album, title=title, cover_path=cover)

    def _convert_to_mp3(self, item: FileItem, decrypted: str) -> None:
        # Snapshot metadata from the tagged file
        saved = read_meta_from_file(decrypted)
        log.debug("[meta] snapshot: artist=%r album=%r title=%r cover=%s",
                  saved.artist, saved.album, saved.title,
                  "yes" if saved.cover_data else "no")

        # ffmpeg: pure audio conversion, no metadata
        # Use temp output to avoid input==output when source is already MP3
        tmp_out = os.path.join(self.output_dir, item.path.stem + ".tmp.mp3")
        final_out = os.path.join(self.output_dir, item.path.stem + ".mp3")
        cmd = [
            "ffmpeg", "-y",
            "-i", decrypted,
            "-map", "0:a",
            "-c:a", "libmp3lame", "-b:a", "320k",
            "-id3v2_version", "3",
            tmp_out,
        ]
        log.info("[ffmpeg] %s -> mp3", item.name)
        log.debug("[ffmpeg] %s", " ".join(cmd))
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        if r.returncode != 0:
            log.error("[ffmpeg] FAILED: %s", r.stderr.strip()[-200:])
            self._update(self.items.index(item), "失败")
            return

        # Replace with temp output
        os.replace(tmp_out, final_out)

        # Write metadata back to final MP3
        write_meta_from_snapshot(final_out, saved)

        # Remove intermediate decrypted file if different
        if os.path.isfile(decrypted) and os.path.realpath(decrypted) != os.path.realpath(final_out):
            os.remove(decrypted)
            log.debug("[cleanup] removed %s", os.path.basename(decrypted))

        self._update(self.items.index(item), "完成")


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

        self._setup_menubar()
        self._setup_ui()
        self._setup_statusbar()
        self.Centre()
        self.Show()

    # ── UI ────────────────────────────────────────────────────


    def _setup_menubar(self) -> None:
        mb = wx.MenuBar()
        file_menu = wx.Menu()
        quit_item = file_menu.Append(wx.ID_EXIT, "退出\tCtrl+Q")
        mb.Append(file_menu, "文件")
        self.SetMenuBar(mb)
        self.Bind(wx.EVT_MENU, self._on_quit, quit_item)

    def _setup_ui(self) -> None:
        panel = wx.Panel(self)
        sizer = wx.BoxSizer(wx.VERTICAL)

        # Toolbar
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

        # Path info
        grid = wx.FlexGridSizer(2, 2, 4, 8)
        grid.AddGrowableCol(1)
        self.lbl_input = wx.StaticText(panel, label="未选择")
        self.lbl_output = wx.StaticText(panel, label="未选择")
        grid.Add(wx.StaticText(panel, label="输入:"), 0, wx.ALIGN_CENTRE_VERTICAL)
        grid.Add(self.lbl_input, 0, wx.EXPAND)
        grid.Add(wx.StaticText(panel, label="输出:"), 0, wx.ALIGN_CENTRE_VERTICAL)
        grid.Add(self.lbl_output, 0, wx.EXPAND)
        sizer.Add(grid, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.EXPAND, 10)

        # Advanced options
        adv_box = wx.StaticBox(panel, label="高级选项")
        adv_sizer = wx.StaticBoxSizer(adv_box, wx.HORIZONTAL)

        self.cb_mp3 = wx.CheckBox(panel, label="转换为 MP3 (iPod 兼容, 320kbps)")
        self.cb_mp3.SetValue(self.settings.get("mp3_convert", False))
        self.cb_fill = wx.CheckBox(panel, label="从目录结构填充元数据")
        self.cb_fill.SetValue(self.settings.get("fill_metadata", False))

        adv_sizer.Add(self.cb_mp3, 0, wx.ALL | wx.ALIGN_CENTRE_VERTICAL, 6)
        adv_sizer.Add(self.cb_fill, 0, wx.ALL | wx.ALIGN_CENTRE_VERTICAL, 6)
        adv_sizer.AddStretchSpacer()

        self.lbl_mroot = wx.StaticText(panel, label="音乐根目录: (自动检测)")
        btn_mroot = wx.Button(panel, label="设置根目录", size=(100, -1))
        adv_sizer.Add(self.lbl_mroot, 0, wx.ALL | wx.ALIGN_CENTRE_VERTICAL, 6)
        adv_sizer.Add(btn_mroot, 0, wx.ALL | wx.ALIGN_CENTRE_VERTICAL, 6)

        sizer.Add(adv_sizer, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.EXPAND, 10)

        # Progress
        self.gauge = wx.Gauge(panel, range=100)
        sizer.Add(self.gauge, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.EXPAND, 10)

        # List
        self.dvlc = dv.DataViewListCtrl(panel)
        self.dvlc.AppendTextColumn("文件名", width=220)
        self.dvlc.AppendTextColumn("所在目录", width=420)
        self.dvlc.AppendTextColumn("状态", width=90, align=wx.ALIGN_CENTER)
        sizer.Add(self.dvlc, 1, wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.EXPAND, 10)

        panel.SetSizer(sizer)

        # Events
        self.btn_input.Bind(wx.EVT_BUTTON, self._on_input)
        self.btn_output.Bind(wx.EVT_BUTTON, self._on_output)
        self.btn_go.Bind(wx.EVT_BUTTON, self._on_go)
        self.btn_clear.Bind(wx.EVT_BUTTON, self._on_clear)
        self.cb_mp3.Bind(wx.EVT_CHECKBOX, self._on_setting)
        self.cb_fill.Bind(wx.EVT_CHECKBOX, self._on_setting)
        btn_mroot.Bind(wx.EVT_BUTTON, self._on_set_mroot)
        self.Bind(wx.EVT_CLOSE, self._on_close)

    def _setup_statusbar(self) -> None:
        self.sb = self.CreateStatusBar()
        self.sb.SetStatusText("就绪")

    # ── Events ────────────────────────────────────────────────

    def _on_input(self, _e) -> None:
        dlg = wx.DirDialog(self, "选择包含 .ncm 文件的目录")
        if dlg.ShowModal() == wx.ID_OK:
            self.input_dir = dlg.GetPath()
            self.lbl_input.SetLabel(self.input_dir)
            self._auto_detect()
            self._scan()
        dlg.Destroy()

    def _on_output(self, _e) -> None:
        dlg = wx.DirDialog(self, "选择输出目录")
        if dlg.ShowModal() == wx.ID_OK:
            self.output_dir = dlg.GetPath()
            self.lbl_output.SetLabel(self.output_dir)
        dlg.Destroy()

    def _on_set_mroot(self, _e) -> None:
        dlg = wx.DirDialog(self, "选择音乐根目录（包含 meta 文件夹）")
        if dlg.ShowModal() == wx.ID_OK:
            self.music_root = dlg.GetPath()
            self.lbl_mroot.SetLabel(f"音乐根目录: {self.music_root}")
        dlg.Destroy()

    def _auto_detect(self) -> None:
        p = Path(self.input_dir)
        for _ in range(5):
            if (p / "meta").is_dir():
                self.music_root = str(p)
                self.lbl_mroot.SetLabel(f"音乐根目录: {self.music_root}")
                return
            p = p.parent
        self.music_root = ""
        self.lbl_mroot.SetLabel("音乐根目录: (未检测到)")

    def _on_setting(self, _e) -> None:
        self.settings["mp3_convert"] = self.cb_mp3.GetValue()
        self.settings["fill_metadata"] = self.cb_fill.GetValue()
        save_settings(self.settings)

    def _scan(self) -> None:
        self._on_clear(None)
        root = Path(self.input_dir)
        if not root.is_dir():
            return
        paths = sorted(root.rglob("*.ncm"))
        self.files = [FileItem(p) for p in paths]
        for item in self.files:
            self.dvlc.AppendItem([item.name, item.directory, item.status])
        log.info("[scan] %d files in %s", len(self.files), self.input_dir)
        self.sb.SetStatusText(f"扫描完成，找到 {len(self.files)} 个文件")

    def _on_clear(self, _e) -> None:
        self.dvlc.DeleteAllItems()
        self.files.clear()
        self.gauge.SetValue(0)
        self.sb.SetStatusText("就绪")
        log.info("[list] cleared")

    def _on_go(self, _e) -> None:
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
            wx.MessageBox("需要 ffmpeg，请先运行 make setup", "错误", wx.OK | wx.ICON_ERROR)
            return

        log.info("=== 开始 ===")
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
            self.sb.SetStatusText(f"完成 {len(self.files)} 个文件")
            log.info("=== 完成 ===")
            return
        self.files[index].status = status
        self.dvlc.SetValue(status, index, 2)
        self.gauge.SetValue(index + 1)

    def _on_quit(self, _e) -> None:
        if self.convert_thread and self.convert_thread.is_alive():
            self.convert_thread.cancel()
        self.Destroy()

    def _on_close(self, _e) -> None:
        self._on_quit(_e)


def main() -> None:
    app = wx.App()
    NcmDumpFrame()
    app.MainLoop()


if __name__ == "__main__":
    main()
