"""Metadata extraction and audio tagging via mutagen + ncm header parsing."""

from __future__ import annotations

import base64
import json
import os
import struct
from pathlib import Path

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from mutagen.flac import FLAC, Picture as FlacPicture
from mutagen.id3 import ID3, APIC, TALB, TIT2, TPE1, TPE2
from mutagen.mp3 import MP3

from logger import log

_CORE_KEY = bytes.fromhex("687A4852416D736F356B496E62617857")
_MODIFY_KEY = bytes.fromhex("2331346C6A6B5F215C5D2630553C2728")


# ── AES helpers ───────────────────────────────────────────────

def _aes_ecb_decrypt(data: bytes, key: bytes) -> bytes:
    cipher = Cipher(algorithms.AES(key), modes.ECB())
    d = cipher.decryptor()
    return d.update(data) + d.finalize()


def _rstrip_nul(data: bytes) -> bytes:
    return data.rstrip(b"\x00")


# ── JSON helper ───────────────────────────────────────────────

def _parse_json_lenient(raw: bytes) -> dict | None:
    """Parse JSON, handling trailing garbage after the root object."""
    s = raw.decode("utf-8", errors="replace")
    # Find balanced root object
    depth = 0
    end = 0
    for i, ch in enumerate(s):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                end = i + 1
                break
    if end > 0:
        s = s[:end]
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        return None


# ── NCM header parsing ────────────────────────────────────────

def extract_ncm_meta(filepath: str | Path) -> dict | None:
    """Read .ncm header and return the embedded JSON metadata dict."""
    try:
        with open(filepath, "rb") as f:
            h1 = struct.unpack("<I", f.read(4))[0]
            h2 = struct.unpack("<I", f.read(4))[0]
            if h1 != 0x4E455443 or h2 != 0x4D414446:
                return None

            f.seek(2, 1)

            key_len = struct.unpack("<I", f.read(4))[0]
            if key_len <= 0:
                return None
            keydata = bytearray(f.read(key_len))
            for i in range(len(keydata)):
                keydata[i] ^= 0x64
            _keydata = _rstrip_nul(_aes_ecb_decrypt(bytes(keydata), _CORE_KEY))

            meta_len = struct.unpack("<I", f.read(4))[0]
            if meta_len <= 0:
                return None
            modify = bytearray(f.read(meta_len))
            for i in range(len(modify)):
                modify[i] ^= 0x63
            modify = modify[22:]
            decoded = base64.b64decode(bytes(modify))
            decrypted = _rstrip_nul(_aes_ecb_decrypt(decoded, _MODIFY_KEY))
            decrypted = decrypted[6:]  # skip "music:"

            return _parse_json_lenient(decrypted)
    except Exception as e:
        log.warning("[metadata] parse failed %s: %s", filepath, e)
        return None


# ── Path-derived metadata ─────────────────────────────────────

def parse_path_meta(filepath: str | Path) -> tuple[str, str, str]:
    """Derive (artist, album, title) from  root/Artist/Album/Song.ncm."""
    p = Path(filepath)
    artist = p.parent.parent.name if p.parent.parent else ""
    album = p.parent.name
    title = p.stem
    return artist, album, title


# ── Cover lookup ──────────────────────────────────────────────

def find_cover(music_id: int, music_root: str | Path) -> str | None:
    if not music_id:
        return None
    cover = Path(music_root) / "meta" / f"track-{music_id}.jpg"
    if cover.is_file():
        log.info("[cover] found: %s", cover.name)
        return str(cover)
    return None


# ── Mutagen tag I/O ───────────────────────────────────────────

def _cover_mime(cover_path: str) -> str:
    return "image/png" if cover_path.lower().endswith(".png") else "image/jpeg"


def _ensure_id3(mp3: MP3) -> ID3:
    if mp3.tags is None:
        mp3.add_tags()
    return mp3.tags


def write_tags(
    audio_path: str,
    artist: str = "",
    album: str = "",
    title: str = "",
    cover_path: str | None = None,
) -> None:
    """Write tags and optionally embed cover art."""
    if not any((artist, album, title)) and not cover_path:
        return

    ext = os.path.splitext(audio_path)[1].lower()
    log.debug("[tag] writing to %s", os.path.basename(audio_path))

    if ext == ".mp3":
        audio = MP3(audio_path)
        tags = _ensure_id3(audio)
        if artist:
            tags.add(TPE1(encoding=3, text=artist))
            tags.add(TPE2(encoding=3, text=artist))
        if album:
            tags.add(TALB(encoding=3, text=album))
        if title:
            tags.add(TIT2(encoding=3, text=title))
        if cover_path:
            tags.delall("APIC")
            with open(cover_path, "rb") as f:
                tags.add(APIC(encoding=3, mime=_cover_mime(cover_path),
                              type=3, desc="Cover", data=f.read()))
        tags.save(v2_version=3)
    elif ext == ".flac":
        audio = FLAC(audio_path)
        if artist:
            audio["artist"] = artist
            audio["albumartist"] = artist
        if album:
            audio["album"] = album
        if title:
            audio["title"] = title
        if cover_path:
            audio.clear_pictures()
            pic = FlacPicture()
            pic.type = 3
            pic.mime = _cover_mime(cover_path)
            pic.desc = "Cover"
            with open(cover_path, "rb") as f:
                pic.data = f.read()
            audio.add_picture(pic)
        audio.save()
    else:
        log.warning("[tag] unsupported format: %s", audio_path)
        return

    log.info("[tag] wrote to %s", os.path.basename(audio_path))


class SavedMeta:
    def __init__(self) -> None:
        self.artist = ""
        self.album = ""
        self.title = ""
        self.cover_data: bytes | None = None
        self.cover_mime = "image/jpeg"


def read_meta_from_file(audio_path: str) -> SavedMeta:
    """Read all tags and cover art into a portable snapshot."""
    m = SavedMeta()
    ext = os.path.splitext(audio_path)[1].lower()
    try:
        if ext == ".mp3":
            tags = ID3(audio_path)
            m.artist = str(tags.get("TPE1", ""))
            m.album = str(tags.get("TALB", ""))
            m.title = str(tags.get("TIT2", ""))
            apic = tags.getall("APIC")
            if apic:
                m.cover_data = apic[0].data
                m.cover_mime = apic[0].mime
        elif ext == ".flac":
            audio = FLAC(audio_path)
            m.artist = str(audio.get("artist", ""))
            m.album = str(audio.get("album", ""))
            m.title = str(audio.get("title", ""))
            pics = audio.pictures
            if pics:
                m.cover_data = pics[0].data
                m.cover_mime = pics[0].mime
    except Exception as e:
        log.warning("[tag] read failed %s: %s", audio_path, e)
    return m


def write_meta_from_snapshot(audio_path: str, meta: SavedMeta) -> None:
    """Apply a SavedMeta snapshot to an audio file."""
    ext = os.path.splitext(audio_path)[1].lower()
    if ext == ".mp3":
        audio = MP3(audio_path)
        tags = _ensure_id3(audio)
        if meta.artist:
            tags.add(TPE1(encoding=3, text=meta.artist))
            tags.add(TPE2(encoding=3, text=meta.artist))
        if meta.album:
            tags.add(TALB(encoding=3, text=meta.album))
        if meta.title:
            tags.add(TIT2(encoding=3, text=meta.title))
        if meta.cover_data:
            tags.delall("APIC")
            tags.add(APIC(encoding=3, mime=meta.cover_mime,
                          type=3, desc="Cover", data=meta.cover_data))
        tags.save(v2_version=3)
    elif ext == ".flac":
        audio = FLAC(audio_path)
        if meta.artist:
            audio["artist"] = meta.artist
            audio["albumartist"] = meta.artist
        if meta.album:
            audio["album"] = meta.album
        if meta.title:
            audio["title"] = meta.title
        if meta.cover_data:
            audio.clear_pictures()
            pic = FlacPicture()
            pic.type = 3
            pic.mime = meta.cover_mime
            pic.desc = "Cover"
            pic.data = meta.cover_data
            audio.add_picture(pic)
        audio.save()
    else:
        log.warning("[tag] unsupported: %s", audio_path)
        return

    log.info("[tag] restored to %s", os.path.basename(audio_path))
