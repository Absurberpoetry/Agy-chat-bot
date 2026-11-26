from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
import os
import re
import struct
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from .models import IncomingMessage, MediaAttachment

logger = logging.getLogger(__name__)

_CONTENT_TYPE_EXT: dict[str, str] = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "image/bmp": ".bmp",
    "image/x-ms-bmp": ".bmp",
    "image/avif": ".avif",
    "image/tiff": ".tiff",
    "image/svg+xml": ".svg",
}

_ALLOWED_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".avif", ".tiff", ".tif", ".svg"}

_DATA_URI_RE = re.compile(r"^data:[^;]+;base64,(.+)$", re.IGNORECASE | re.DOTALL)
_MAX_IMAGE_BYTES = 20 * 1024 * 1024
_DEFAULT_CACHE_MAX_FILES = 400
_DEFAULT_CACHE_MAX_BYTES = 300 * 1024 * 1024


def looks_like_image_bytes(data: bytes) -> bool:
    """Return True if raw bytes start with a recognized image magic signature."""
    if not data or len(data) < 4:
        return False
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return True
    if data.startswith(b"\xff\xd8\xff"):
        return True
    if data.startswith((b"GIF87a", b"GIF89a")):
        return True
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return True
    if data.startswith(b"BM"):
        return True
    if data.startswith((b"II*\x00", b"MM\x00*")):
        return True
    if len(data) >= 12 and data[4:8] == b"ftyp" and b"avif" in data[8:16]:
        return True
    head_text = data[:256].lstrip()
    if head_text.startswith(b"<svg") or (head_text.startswith(b"<?xml") and b"<svg" in data[:512]):
        return True
    return False


def detect_image_ext(data: bytes, content_type: str = "", hint_name: str = "") -> str:
    """Detect image file extension from magic bytes, Content-Type, or filename hint."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if data.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return ".gif"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    if data.startswith(b"BM"):
        return ".bmp"
    if data.startswith((b"II*\x00", b"MM\x00*")):
        return ".tiff"
    if len(data) >= 12 and data[4:8] == b"ftyp" and b"avif" in data[8:16]:
        return ".avif"
    head_text = data[:256].lstrip()
    if head_text.startswith(b"<svg") or (head_text.startswith(b"<?xml") and b"<svg" in data[:512]):
        return ".svg"

    ct = (content_type or "").split(";", 1)[0].strip().lower()
    if ct in _CONTENT_TYPE_EXT:
        return _CONTENT_TYPE_EXT[ct]

    if hint_name:
        clean_hint = hint_name.split("?", 1)[0].split("#", 1)[0]
        ext = Path(clean_hint).suffix.lower()
        if ext in _ALLOWED_EXTS:
            return ".jpg" if ext == ".jpeg" else ext

    return ".jpg"


_PNG_KEEP_CHUNKS = {b"IHDR", b"PLTE", b"tRNS", b"IDAT", b"IEND"}
_STANDARD_JFIF_APP0 = b"\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"


def sanitize_png_chunks(data: bytes) -> bytes:
    """Strip ancillary chunks (Android Skia eXIf/iCCP/sBIT/tEXt) from PNG bytes cleanly."""
    sig = b"\x89PNG\r\n\x1a\n"
    if not data.startswith(sig):
        return data
    out = bytearray(sig)
    pos = len(sig)
    n = len(data)
    has_ihdr = False
    has_idat = False
    has_iend = False
    while pos + 12 <= n:
        length = struct.unpack(">I", data[pos : pos + 4])[0]
        ctype = data[pos + 4 : pos + 8]
        chunk_end = pos + 12 + length
        if chunk_end > n:
            return data
        if ctype in _PNG_KEEP_CHUNKS:
            out.extend(data[pos:chunk_end])
            if ctype == b"IHDR":
                has_ihdr = True
            elif ctype == b"IDAT":
                has_idat = True
            elif ctype == b"IEND":
                has_iend = True
                break
        pos = chunk_end
    if has_ihdr and has_idat and has_iend:
        return bytes(out)
    return data


def sanitize_jpeg_segments(data: bytes) -> bytes:
    """Strip Android/EXIF/XMP/ICC APP1..APP15 & COM segments from JPEG bytes and ensure JFIF APP0."""
    if not data.startswith(b"\xff\xd8"):
        return data
    n = len(data)
    pos = 2
    segments: list[bytes] = []
    has_app0 = False
    found_sos = False
    while pos + 1 < n:
        if data[pos] != 0xFF:
            return data
        while pos < n and data[pos] == 0xFF:
            pos += 1
        if pos >= n:
            break
        marker = data[pos]
        pos += 1
        if marker == 0xD9:  # EOI
            segments.append(b"\xff\xd9")
            break
        if marker == 0xDA:  # SOS -> rest of stream is scan data + EOI
            segments.append(b"\xff\xda" + data[pos:])
            found_sos = True
            break
        if 0xD0 <= marker <= 0xD7 or marker == 0x01:
            segments.append(bytes([0xFF, marker]))
            continue
        if pos + 2 > n:
            return data
        seg_len = struct.unpack(">H", data[pos : pos + 2])[0]
        if seg_len < 2 or pos + seg_len > n:
            return data
        seg_bytes = bytes([0xFF, marker]) + data[pos : pos + seg_len]
        pos += seg_len
        if (0xE1 <= marker <= 0xEF and marker != 0xEE) or marker == 0xFE:
            # Drop EXIF/XMP/ICC/COM metadata segments (preserve APP14 0xEE for Adobe CMYK/YCCK color transform)
            continue
        if marker == 0xE0:
            has_app0 = True
        segments.append(seg_bytes)
    if not found_sos:
        return data
    out = bytearray(b"\xff\xd8")
    if not has_app0:
        out.extend(_STANDARD_JFIF_APP0)
    for seg in segments:
        out.extend(seg)
    return bytes(out)


def sanitize_reference_image(data: bytes, hint_name: str = "") -> tuple[bytes, str]:
    """Normalize a local reference image into clean standard PNG/JPG/WEBP/GIF bytes + verified extension."""
    ext = detect_image_ext(data, hint_name=hint_name)
    if ext == ".png":
        return sanitize_png_chunks(data), ".png"
    if ext == ".jpg":
        return sanitize_jpeg_segments(data), ".jpg"
    if ext in (".webp", ".gif"):
        return data, ext
    return data, ext


def _decode_base64_source(src: str) -> bytes | None:
    s = (src or "").strip()
    if not s:
        return None
    b64_payload = ""
    if s.lower().startswith("base64://"):
        b64_payload = s[len("base64://") :].strip()
    else:
        m = _DATA_URI_RE.match(s)
        if m:
            b64_payload = m.group(1).strip()
    if not b64_payload:
        return None
    compact = re.sub(r"\s+", "", b64_payload)
    if len(compact) > int(_MAX_IMAGE_BYTES * 1.4):
        logger.warning("拒绝解码超大 base64 图片负载 (%d chars)", len(compact))
        return None
    try:
        return base64.b64decode(compact, validate=True)
    except Exception:
        return None


def _resolve_local_file(src: str) -> Path | None:
    s = (src or "").strip()
    if not s:
        return None
    if s.lower().startswith(("http://", "https://", "base64://", "data:")):
        return None
    if s.lower().startswith("file://"):
        parsed = urlparse(s)
        path_str = unquote(parsed.path or "")
        # On Windows file:///C:/path -> /C:/path
        if os.name == "nt" and len(path_str) >= 3 and path_str[0] == "/" and path_str[2] == ":":
            path_str = path_str[1:]
        candidate = Path(path_str)
    else:
        candidate = Path(s)
    try:
        if candidate.is_file():
            return candidate
    except OSError:
        return None
    return None


def _is_http_url(src: str) -> bool:
    s = (src or "").strip().lower()
    return s.startswith("http://") or s.startswith("https://")


async def _fetch_http_bytes(
    url: str,
    *,
    proxy: str = "",
    timeout_seconds: float = 60.0,
    max_bytes: int = _MAX_IMAGE_BYTES,
) -> tuple[bytes | None, str]:
    try:
        import aiohttp
    except ImportError:
        logger.warning("缺少 aiohttp 依赖，无法下载图片: %s", url)
        return None, ""

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
        )
    }
    timeout = aiohttp.ClientTimeout(total=timeout_seconds)

    async def _attempt(*, use_proxy: str | None, verify_ssl: bool) -> tuple[bytes | None, str]:
        connector = aiohttp.TCPConnector(ssl=verify_ssl)
        async with aiohttp.ClientSession(timeout=timeout, connector=connector, trust_env=False) as session:
            async with session.get(url, headers=headers, proxy=use_proxy, allow_redirects=True) as resp:
                if resp.status != 200:
                    logger.debug("图片下载 HTTP %s: %s", resp.status, url)
                    return None, ""
                cl = resp.headers.get("Content-Length")
                if cl:
                    try:
                        if int(cl) > max_bytes:
                            logger.warning("图片响应体积超限 (%s > %d): %s", cl, max_bytes, url)
                            return None, ""
                    except ValueError:
                        pass
                buf = bytearray()
                async for chunk in resp.content.iter_chunked(65536):
                    buf.extend(chunk)
                    if len(buf) > max_bytes:
                        logger.warning("图片流式下载超过体积上限 (%d bytes): %s", max_bytes, url)
                        return None, ""
                if not buf:
                    return None, ""
                return bytes(buf), str(resp.headers.get("Content-Type", ""))

    # Try direct first (QQ domestic CDN doesn't need proxy); retry without SSL verification on cert errors;
    # finally fallback to configured proxy if direct failed.
    attempts: list[tuple[str | None, bool]] = [(None, True)]
    if proxy.strip():
        attempts.append((proxy.strip(), True))

    for use_proxy, verify_ssl in attempts:
        try:
            data, ct = await _attempt(use_proxy=use_proxy, verify_ssl=verify_ssl)
            if data:
                return data, ct
        except Exception as exc:
            if verify_ssl and "ssl" in str(exc).lower():
                try:
                    data, ct = await _attempt(use_proxy=use_proxy, verify_ssl=False)
                    if data:
                        return data, ct
                except Exception as exc2:
                    logger.debug("图片下载失败 (ssl=False, proxy=%s): %s -> %s", bool(use_proxy), url, exc2)
            else:
                logger.debug("图片下载失败 (proxy=%s): %s -> %s", bool(use_proxy), url, exc)

    return None, ""


def prune_media_cache(
    cache_dir: Path,
    *,
    max_files: int = _DEFAULT_CACHE_MAX_FILES,
    max_bytes: int = _DEFAULT_CACHE_MAX_BYTES,
) -> int:
    """Evict oldest cached media files when file count or total disk usage exceeds thresholds."""
    if not cache_dir.is_dir():
        return 0
    now = time.time()
    entries: list[tuple[float, int, Path]] = []
    removed = 0
    try:
        for p in cache_dir.iterdir():
            if not p.is_file():
                continue
            try:
                st = p.stat()
            except OSError:
                continue
            if p.name.endswith(".tmp"):
                if now - st.st_mtime > 600:
                    try:
                        p.unlink(missing_ok=True)
                        removed += 1
                    except OSError:
                        pass
                continue
            entries.append((st.st_mtime, st.st_size, p))
    except OSError:
        return removed

    total_bytes = sum(sz for _, sz, _ in entries)
    if len(entries) <= max_files and total_bytes <= max_bytes:
        return removed

    entries.sort(key=lambda item: item[0])  # oldest first
    remaining_files = len(entries)
    for _, sz, path in entries:
        if remaining_files <= max_files and total_bytes <= max_bytes:
            break
        try:
            path.unlink(missing_ok=True)
            removed += 1
            remaining_files -= 1
            total_bytes -= sz
        except OSError:
            continue
    return removed


def _write_cached_image(
    data: bytes,
    *,
    workspace_dir: Path,
    cache_subdir: str,
    content_type: str = "",
    hint_name: str = "",
) -> str:
    ext = detect_image_ext(data, content_type=content_type, hint_name=hint_name)
    digest = hashlib.sha256(data).hexdigest()[:16]
    cache_dir = workspace_dir / cache_subdir
    cache_dir.mkdir(parents=True, exist_ok=True)
    filename = f"img_{digest}{ext}"
    target = cache_dir / filename
    if not target.exists():
        tmp = cache_dir / f".{filename}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp"
        tmp.write_bytes(data)
        os.replace(tmp, target)
        prune_media_cache(cache_dir)
    return f"{cache_subdir}/{filename}"


async def materialize_image(
    att: MediaAttachment,
    *,
    workspace_dir: Path,
    adapter: Any = None,
    proxy: str = "",
    cache_subdir: str = ".media_cache",
) -> str | None:
    """Ensure an image MediaAttachment exists inside `workspace_dir/.media_cache` and set `att.local_path`."""
    if att.kind != "image":
        return None

    if att.local_path:
        existing = workspace_dir / att.local_path
        if existing.is_file():
            return att.local_path

    hint = att.file_name or att.file_id or att.url
    data: bytes | None = None
    content_type = ""

    # 1) Direct base64 payload in file_id or url
    for candidate in (att.file_id, att.url):
        decoded = _decode_base64_source(candidate)
        if decoded and looks_like_image_bytes(decoded):
            data = decoded
            break

    # 2) Direct local file path (e.g., NapCat local cache path)
    if data is None:
        for candidate in (att.source_path, att.file_id, att.url):
            local_p = _resolve_local_file(candidate)
            if local_p is not None:
                try:
                    if local_p.stat().st_size > _MAX_IMAGE_BYTES:
                        logger.debug("跳过超大本地图片文件 %s", local_p)
                        continue
                    raw = local_p.read_bytes()
                    if looks_like_image_bytes(raw):
                        data = raw
                        hint = local_p.name
                        break
                except OSError as exc:
                    logger.debug("读取本地图片文件失败 %s: %s", local_p, exc)

    # 3) Direct HTTP/HTTPS download from attachment url
    if data is None and _is_http_url(att.url):
        fetched, ct = await _fetch_http_bytes(att.url, proxy=proxy)
        if fetched and looks_like_image_bytes(fetched):
            data = fetched
            content_type = ct

    # 4) Fallback to adapter RPC (e.g., NapCat get_image) for refreshed URL / local file / base64
    if data is None and adapter is not None and hasattr(adapter, "resolve_image_source"):
        try:
            resolved = await adapter.resolve_image_source(att)
        except Exception as exc:
            logger.debug("适配器解析图片源失败: %s", exc)
            resolved = {}
        if isinstance(resolved, dict):
            b64_val = str(resolved.get("base64") or "").strip()
            if b64_val:
                if not b64_val.lower().startswith(("base64://", "data:")):
                    b64_val = f"base64://{b64_val}"
                decoded = _decode_base64_source(b64_val)
                if decoded and looks_like_image_bytes(decoded):
                    data = decoded
            if data is None:
                local_p = _resolve_local_file(str(resolved.get("file") or ""))
                if local_p is not None:
                    try:
                        if local_p.stat().st_size <= _MAX_IMAGE_BYTES:
                            raw = local_p.read_bytes()
                            if looks_like_image_bytes(raw):
                                data = raw
                                hint = local_p.name
                    except OSError:
                        pass
            if data is None:
                fresh_url = str(resolved.get("url") or "").strip()
                if _is_http_url(fresh_url) and fresh_url != att.url:
                    fetched, ct = await _fetch_http_bytes(fresh_url, proxy=proxy)
                    if fetched and looks_like_image_bytes(fetched):
                        data = fetched
                        content_type = ct

    if not data:
        return None

    rel_path = _write_cached_image(
        data,
        workspace_dir=workspace_dir,
        cache_subdir=cache_subdir,
        content_type=content_type,
        hint_name=hint,
    )
    att.local_path = rel_path
    return rel_path


async def materialize_snapshot_images(
    messages: list[IncomingMessage],
    *,
    workspace_dir: Path,
    adapter: Any = None,
    proxy: str = "",
    cache_subdir: str = ".media_cache",
    concurrency: int = 8,
) -> list[str]:
    """Download/materialize ALL image attachments across all messages in a snapshot (no count limit)."""
    targets: list[MediaAttachment] = []
    for msg in messages:
        for att in msg.attachments:
            if att.kind == "image":
                targets.append(att)

    if not targets:
        return []

    sem = asyncio.Semaphore(max(1, concurrency))

    async def _run_one(att: MediaAttachment) -> str | None:
        async with sem:
            return await materialize_image(
                att,
                workspace_dir=workspace_dir,
                adapter=adapter,
                proxy=proxy,
                cache_subdir=cache_subdir,
            )

    results = await asyncio.gather(*(_run_one(a) for a in targets), return_exceptions=True)
    paths: list[str] = []
    for res in results:
        if isinstance(res, str) and res:
            paths.append(res)
    return paths
