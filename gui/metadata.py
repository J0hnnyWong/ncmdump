"""Metadata extraction and audio tagging via mutagen + ncm header parsing."""

from __future__ import annotations

import base64
import json
import os
import struct
from pathlib import Path

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from mutagen import File as MutagenFile
from mutagen.flac import Picture as FlacPicture
from mutagen.id3 import ID3, APIC, TIT2, TPE1, TPE2, TALB

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


# ── NCM header parsing ────────────────────────────────────────

def extract_ncm_meta(filepath: str | Path) -> dict | None:
    """Read .ncm header and return the embedded JSON metadata dict.

    Contains keys like musicId, musicName, artist, album, format, etc.
    """
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

            return json.loads(decrypted.decode("utf-8", errors="replace"))
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
    """Look for track-{id}.jpg inside <music_root>/meta/."""
    if not music_id:
        return None
    cover = Path(music_root) / "meta" / f"track-{music_id}.jpg"
    if cover.is_file():
        log.info("[cover] found: %s", cover.name)
        return str(cover)
    return None


# ── Mutagen helpers ───────────────────────────────────────────

def _cover_mime(cover_path: str) -> str:
    ext = os.path.splitext(cover_path)[1].lower()
    if ext == ".png":
        return "image/png"
    return "image/jpeg"


def _write_cover_mutagen(audio_path: str, cover_path: str) -> None:
    """Embed cover art into an audio file using mutagen."""
    with open(cover_path, "rb") as f:
        cover_data = f.read()
    mime = _cover_mime(cover_path)

    audio = MutagenFile(audio_path)
    if audio is None:
        log.warning("[tag] unsupported format: %s", audio_path)
        return

    ext = os.path.splitext(audio_path)[1].lower()
    if ext == ".mp3":
        # ID3
        id3 = ID3(audio_path)
        id3.delall("APIC")
        id3.add(APIC(encoding=3, mime=mime, type=3, desc="Cover", data=cover_data))
        id3.save()
    elif ext == ".flac":
        pic = FlacPicture()
        pic.type = 3  # front cover
        pic.mime = mime
        pic.desc = "Cover"
        pic.data = cover_data
        audio.clear_pictures()
        audio.add_picture(pic)
        audio.save()
    else:
        log.warning("[tag] cover not supported for: %s", audio_path)


def write_tags(
    audio_path: str,
    artist: str = "",
    album: str = "",
    title: str = "",
    cover_path: str | None = None,
) -> None:
    """Write tags (and optionally cover) to an audio file via mutagen."""
    if not any((artist, album, title)) and not cover_path:
        return

    audio = MutagenFile(audio_path)
    if audio is None:
        log.warning("[tag] unsupported: %s", audio_path)
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
        _write_cover_mutagen(audio_path, cover_path)

    log.info("[tag] wrote to %s", os.path.basename(audio_path))


class SavedMeta:
    """Metadata snapshot for re-application after audio conversion."""
    def __init__(self) -> None:
        self.artist = ""
        self.album = ""
        self.title = ""
        self.cover_data: bytes | None = None
        self.cover_mime = "image/jpeg"


def read_meta_from_file(audio_path: str) -> SavedMeta:
    """Read all tags and embedded cover from an audio file into memory."""
    m = SavedMeta()
    try:
        audio = MutagenFile(audio_path)
        if audio is None:
            return m
        m.artist = str(audio.get("artist", ""))
        m.album = str(audio.get("album", ""))
        m.title = str(audio.get("title", ""))
    except Exception:
        pass

    # Extract cover
    try:
        ext = os.path.splitext(audio_path)[1].lower()
        if ext == ".mp3":
            id3 = ID3(audio_path)
            apic_list = id3.getall("APIC")
            if apic_list:
                m.cover_data = apic_list[0].data
                m.cover_mime = apic_list[0].mime
        elif ext == ".flac":
            audio = MutagenFile(audio_path)
            pics = getattr(audio, "pictures", [])
            if pics:
                m.cover_data = pics[0].data
                m.cover_mime = pics[0].mime
    except Exception:
        pass

    return m


def write_meta_from_snapshot(audio_path: str, meta: SavedMeta) -> None:
    """Apply a SavedMeta snapshot back to an audio file."""
    audio = MutagenFile(audio_path)
    if audio is None:
        log.warning("[tag] unsupported: %s", audio_path)
        return

    if meta.artist:
        audio["artist"] = meta.artist
        audio["albumartist"] = meta.artist
    if meta.album:
        audio["album"] = meta.album
    if meta.title:
        audio["title"] = meta.title
    audio.save()

    # Re-embed cover
    if meta.cover_data:
        ext = os.path.splitext(audio_path)[1].lower()
        if ext == ".mp3":
            id3 = ID3(audio_path)
            id3.delall("APIC")
            id3.add(APIC(encoding=3, mime=meta.cover_mime, type=3, desc="Cover", data=meta.cover_data))
            id3.save()
        elif ext == ".flac":
            pic = FlacPicture()
            pic.type = 3
            pic.mime = meta.cover_mime
            pic.desc = "Cover"
            pic.data = meta.cover_data
            audio = MutagenFile(audio_path)
            audio.clear_pictures()
            audio.add_picture(pic)
            audio.save()

    log.info("[tag] restored meta to %s", os.path.basename(audio_path))
