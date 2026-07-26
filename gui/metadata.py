"""Extract metadata from .ncm files and directory structure."""

from __future__ import annotations

import base64
import json
import struct
from pathlib import Path

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from logger import log

_CORE_KEY = bytes.fromhex("687A4852416D736F356B496E62617857")
_MODIFY_KEY = bytes.fromhex("2331346C6A6B5F215C5D2630553C2728")


def _aes_ecb_decrypt(data: bytes, key: bytes) -> bytes:
    cipher = Cipher(algorithms.AES(key), modes.ECB())
    d = cipher.decryptor()
    return d.update(data) + d.finalize()


def _rstrip_nul(data: bytes) -> bytes:
    return data.rstrip(b"\x00")


def extract_ncm_meta(filepath: str | Path) -> dict | None:
    """Read .ncm header and return the embedded JSON metadata dict.

    Returns None if the file is not a valid .ncm or parsing fails.
    The dict contains keys like musicId, musicName, artist, album, format, etc.
    """
    try:
        with open(filepath, "rb") as f:
            h1 = struct.unpack("<I", f.read(4))[0]
            h2 = struct.unpack("<I", f.read(4))[0]
            if h1 != 0x4E455443 or h2 != 0x4D414446:  # "CTENFDAM"
                log.debug("[metadata] %s: not a valid NCM header", filepath)
                return None

            f.seek(2, 1)  # skip gap

            # --- key section ---
            key_len = struct.unpack("<I", f.read(4))[0]
            if key_len <= 0:
                return None
            keydata = bytearray(f.read(key_len))
            for i in range(len(keydata)):
                keydata[i] ^= 0x64
            keydata = _rstrip_nul(_aes_ecb_decrypt(bytes(keydata), _CORE_KEY))
            # keydata = "neteasecloudmusic" + rc4_key

            # --- metadata section ---
            meta_len = struct.unpack("<I", f.read(4))[0]
            if meta_len <= 0:
                return None
            modify = bytearray(f.read(meta_len))
            for i in range(len(modify)):
                modify[i] ^= 0x63

            # "163 key(Don't modify):" = 22 bytes
            modify = modify[22:]
            decoded = base64.b64decode(bytes(modify))
            decrypted = _rstrip_nul(_aes_ecb_decrypt(decoded, _MODIFY_KEY))
            decrypted = decrypted[6:]  # skip "music:"

            meta = json.loads(decrypted.decode("utf-8", errors="replace"))
            log.debug("[metadata] %s: musicId=%s", filepath, meta.get("musicId"))
            return meta
    except Exception as e:
        log.warning("[metadata] failed to parse %s: %s", filepath, e)
        return None


def parse_path_meta(filepath: str | Path) -> tuple[str, str, str]:
    """Derive (artist, album, title) from the file's directory structure.

    Expects:  root / Artist / Album / Song.ncm
    Returns empty strings for any missing parts.
    """
    p = Path(filepath)
    title = p.stem  # filename without .ncm
    artist = p.parent.parent.name if p.parent.parent else ""
    album = p.parent.name
    return artist, album, title


def find_cover(music_id: int, music_root: str | Path) -> str | None:
    """Look for track-{music_id}.jpg in <music_root>/meta/."""
    if not music_id:
        return None
    cover = Path(music_root) / "meta" / f"track-{music_id}.jpg"
    if cover.is_file():
        log.debug("[metadata] found cover: %s", cover)
        return str(cover)
    log.debug("[metadata] no cover for musicId=%s", music_id)
    return None


def build_ffmpeg_meta_cmd(
    input_file: str,
    output_file: str,
    artist: str = "",
    album: str = "",
    title: str = "",
    cover: str | None = None,
    mp3_bitrate: str = "320k",
) -> list[str]:
    """Build an ffmpeg command that sets tags, optionally embeds cover, and
    re-encodes to the target format/bitrate."""
    cmd = ["ffmpeg", "-y", "-i", input_file]
    if cover:
        cmd += ["-i", cover]

    cmd += ["-map", "0:a"]
    if cover:
        cmd += ["-map", "1:v"]

    cmd += ["-c:a", "libmp3lame", "-b:a", mp3_bitrate]
    if cover:
        cmd += ["-disposition:v", "attached_pic"]

    if artist:
        cmd += ["-metadata", f"artist={artist}"]
        cmd += ["-metadata", f"album_artist={artist}"]
    if album:
        cmd += ["-metadata", f"album={album}"]
    if title:
        cmd += ["-metadata", f"title={title}"]

    # iPod-compatible
    cmd += ["-id3v2_version", "3", "-write_id3v1", "1"]

    cmd.append(output_file)
    return cmd
