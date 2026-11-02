"""Read-only release metadata from the published APK, never from future Gradle edits."""
from __future__ import annotations

import hashlib
import re
import struct
import threading
import zipfile
from pathlib import Path

PACKAGE = "com.arupbairagya.jupiter"
PUBLIC_ORIGIN = "https://jupiter.example.invalid"
_lock = threading.Lock()
_cache: dict = {}


def _length(data: bytes, offset: int, utf8: bool) -> tuple[int, int]:
    if utf8:
        value = data[offset]
        return (((value & 127) << 8) | data[offset + 1], offset + 2) if value & 128 else (value, offset + 1)
    value = struct.unpack_from("<H", data, offset)[0]
    return (((value & 32767) << 16) | struct.unpack_from("<H", data, offset + 2)[0], offset + 4) if value & 32768 else (value, offset + 2)


def apk_identity(path: Path) -> dict:
    """Read only the root manifest attributes from Android's binary XML format."""
    with zipfile.ZipFile(path) as archive:
        info = archive.getinfo("AndroidManifest.xml")
        if info.file_size > 2_000_000:
            raise ValueError("Manifest too large")
        data = archive.read(info)
    chunk_type, header_size, total = struct.unpack_from("<HHI", data)
    if chunk_type != 3 or total != len(data) or header_size < 8:
        raise ValueError("Invalid binary manifest")
    strings: list[str] = []
    offset = header_size
    while offset + 8 <= total:
        kind, header, size = struct.unpack_from("<HHI", data, offset)
        if size < header or header < 8 or offset + size > total:
            raise ValueError("Invalid manifest chunk")
        if kind == 1:
            count, _, flags, start, _ = struct.unpack_from("<IIIII", data, offset + 8)
            if count > 100_000:
                raise ValueError("Invalid string pool")
            for index in range(count):
                pos = offset + start + struct.unpack_from("<I", data, offset + header + index * 4)[0]
                utf8 = bool(flags & 256)
                length, pos = _length(data, pos, utf8)
                if utf8:
                    length, pos = _length(data, pos, True)
                end = pos + length * (1 if utf8 else 2)
                if end > offset + size:
                    raise ValueError("Invalid manifest string")
                strings.append(data[pos:end].decode("utf-8" if utf8 else "utf-16le"))
        elif kind == 0x102:
            base = offset + header
            _, name, attr_start, attr_size, count = struct.unpack_from("<IIHHH", data, base)
            if strings[name] == "manifest":
                if attr_size < 20:
                    raise ValueError("Invalid manifest attributes")
                values = {}
                for index in range(count):
                    pos = base + attr_start + index * attr_size
                    if pos + 20 > offset + size:
                        raise ValueError("Invalid manifest attribute")
                    _, key, raw, _, _, value_type, value = struct.unpack_from("<IIIHBBI", data, pos)
                    values[strings[key]] = strings[raw] if raw != 0xFFFFFFFF else strings[value] if value_type == 3 else value
                return {"package": values.get("package"), "version_name": values.get("versionName"),
                        "version_code": values.get("versionCode")}
        offset += size
    raise ValueError("Manifest root unavailable")


def _digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def published_android_release(downloads: Path, version: str) -> dict:
    """Advertise only when the immutable APK and mutable alias contain the same bytes."""
    unavailable = {"available": False}
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        return unavailable
    immutable, alias = downloads / f"Jupiter-{version}.apk", downloads / "app-release.apk"
    try:
        key = (str(downloads.resolve()), version, *((p.stat().st_size, p.stat().st_mtime_ns) for p in (immutable, alias)))
        with _lock:
            if _cache.get("key") == key:
                return dict(_cache["value"])
            identity = apk_identity(immutable)
            code = identity["version_code"]
            if identity["package"] != PACKAGE or identity["version_name"] != version or not isinstance(code, int) or code <= 0:
                return unavailable
            digest = _digest(immutable)
            if immutable.stat().st_size != alias.stat().st_size or digest != _digest(alias):
                return unavailable
            # A publisher updating either file mid-read must not advertise a mixed release.
            after = (str(downloads.resolve()), version, *((p.stat().st_size, p.stat().st_mtime_ns) for p in (immutable, alias)))
            if key != after:
                return unavailable
            result = {"available": True, **identity, "sha256": digest,
                      "url": f"{PUBLIC_ORIGIN}/downloads/Jupiter-{version}.apk"}
            _cache.update(key=key, value=result)
            return dict(result)
    except (OSError, ValueError, KeyError, IndexError, struct.error, UnicodeError, zipfile.BadZipFile):
        return unavailable
