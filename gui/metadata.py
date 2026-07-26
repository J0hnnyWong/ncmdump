"""Metadata extraction and audio tagging via mutagen + ncm header parsing."""

from __future__ import annotations

import base64
import json
import os
import struct
import traceback
from pathlib import Path

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from mutagen import File as MutagenFile
from mutagen.flac import Picture as FlacPicture
from mutagen.id3 import ID3, APIC, TALB, TIT2, TPE1, TPE2

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


# ── JSON ──────────────────────────────────────────────────────

def _parse_json_lenient(raw: bytes) -> dict | None:
    s = raw.decode("utf-8", errors="replace")
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


# ── NCM header ────────────────────────────────────────────────

def extract_ncm_meta(filepath: str | Path) -> dict | None:
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
            decrypted = decrypted[6:]
            return _parse_json_lenient(decrypted)
    except Exception:
        log.debug("[metadata] parse exception:\n%s", traceback.format_exc())
        return None


# ── Path metadata ─────────────────────────────────────────────

def parse_path_meta(filepath: str | Path) -> tuple[str, str, str]:
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


def _read_cover_data(cover_path: str) -> bytes:
    with open(cover_path, "rb") as f:
        return f.read()


def _write_id3_tags(filepath: str, artist: str, album: str, title: str,
                    cover_path: str | None) -> None:
    """Write ID3v2.3 tags to an MP3 file."""
    id3 = ID3()
    if artist:
        id3.add(TPE1(encoding=3, text=artist))
        id3.add(TPE2(encoding=3, text=artist))
    if album:
        id3.add(TALB(encoding=3, text=album))
    if title:
        id3.add(TIT2(encoding=3, text=title))
    if cover_path:
        id3.add(APIC(encoding=3, mime=_cover_mime(cover_path),
                     type=3, desc="Cover", data=_read_cover_data(cover_path)))
    id3.save(filepath, v2_version=3)


def _write_flac_tags(filepath: str, artist: str, album: str, title: str,
                     cover_path: str | None) -> None:
    """Write Vorbis comments + cover to a FLAC file."""
    audio = MutagenFile(filepath)
    if audio is None:
        return
    if artist:
        audio["artist"] = artist
        audio["albumartist"] = artist
    if album:
        audio["album"] = album
    if title:
        audio["title"] = title
    audio.save()
    if cover_path:
        audio = MutagenFile(filepath)
        audio.clear_pictures()
        pic = FlacPicture()
        pic.type = 3
        pic.mime = _cover_mime(cover_path)
        pic.desc = "Cover"
        pic.data = _read_cover_data(cover_path)
        audio.add_picture(pic)
        audio.save()


def write_tags(
    audio_path: str,
    artist: str = "",
    album: str = "",
    title: str = "",
    cover_path: str | None = None,
) -> None:
    if not any((artist, album, title)) and not cover_path:
        return
    ext = os.path.splitext(audio_path)[1].lower()
    log.debug("[tag] writing to %s", os.path.basename(audio_path))
    try:
        if ext == ".mp3":
            _write_id3_tags(audio_path, artist, album, title, cover_path)
        elif ext == ".flac":
            _write_flac_tags(audio_path, artist, album, title, cover_path)
        else:
            log.warning("[tag] unsupported format: %s", audio_path)
            return
        log.info("[tag] wrote to %s", os.path.basename(audio_path))
    except Exception:
        log.error("[tag] write failed for %s:\n%s", audio_path, traceback.format_exc())
        raise


# ── Metadata snapshot for convert-then-restore ────────────────

class SavedMeta:
    def __init__(self) -> None:
        self.artist = ""
        self.album = ""
        self.title = ""
        self.cover_data: bytes | None = None
        self.cover_mime = "image/jpeg"


def read_meta_from_file(audio_path: str) -> SavedMeta:
    m = SavedMeta()
    try:
        audio = MutagenFile(audio_path)
        if audio is not None:
            m.artist = str(audio.get("artist", ""))
            m.album = str(audio.get("album", ""))
            m.title = str(audio.get("title", ""))
    except Exception:
        log.debug("[tag] read via File failed:\n%s", traceback.format_exc())

    # Extract cover via format-specific API
    ext = os.path.splitext(audio_path)[1].lower()
    try:
        if ext == ".mp3":
            id3 = ID3(audio_path)
            apic = id3.getall("APIC")
            if apic:
                m.cover_data = apic[0].data
                m.cover_mime = apic[0].mime
        elif ext == ".flac":
            audio = MutagenFile(audio_path)
            if audio and hasattr(audio, "pictures") and audio.pictures:
                m.cover_data = audio.pictures[0].data
                m.cover_mime = audio.pictures[0].mime
    except Exception:
        log.debug("[tag] cover read failed:\n%s", traceback.format_exc())

    return m


def write_meta_from_snapshot(audio_path: str, meta: SavedMeta) -> None:
    ext = os.path.splitext(audio_path)[1].lower()
    try:
        if ext == ".mp3":
            _apply_snapshot_mp3(audio_path, meta)
        elif ext == ".flac":
            _apply_snapshot_flac(audio_path, meta)
        else:
            log.warning("[tag] unsupported: %s", audio_path)
            return
        log.info("[tag] restored to %s", os.path.basename(audio_path))
    except Exception:
        log.error("[tag] restore failed for %s:\n%s", audio_path, traceback.format_exc())
        raise


def _apply_snapshot_mp3(filepath: str, meta: SavedMeta) -> None:
    id3 = ID3()
    if meta.artist:
        id3.add(TPE1(encoding=3, text=meta.artist))
        id3.add(TPE2(encoding=3, text=meta.artist))
    if meta.album:
        id3.add(TALB(encoding=3, text=meta.album))
    if meta.title:
        id3.add(TIT2(encoding=3, text=meta.title))
    if meta.cover_data:
        id3.add(APIC(encoding=3, mime=meta.cover_mime, type=3,
                     desc="Cover", data=meta.cover_data))
    id3.save(filepath, v2_version=3)


def _apply_snapshot_flac(filepath: str, meta: SavedMeta) -> None:
    audio = MutagenFile(filepath)
    if audio is None:
        return
    if meta.artist:
        audio["artist"] = meta.artist
        audio["albumartist"] = meta.artist
    if meta.album:
        audio["album"] = meta.album
    if meta.title:
        audio["title"] = meta.title
    audio.save()
    if meta.cover_data:
        audio = MutagenFile(filepath)
        audio.clear_pictures()
        pic = FlacPicture()
        pic.type = 3
        pic.mime = meta.cover_mime
        pic.desc = "Cover"
        pic.data = meta.cover_data
        audio.add_picture(pic)
        audio.save()
