from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import json
import logging
import os
import random
import time
import urllib.parse
import uuid
from pathlib import Path
from typing import Any

import aiohttp

from ..config import PlatformConfig
from ..models import IncomingMessage, MediaAttachment, Segment
from .base import MessageLookupFn, OnMessageFn, PlatformAdapter, WsConnectFn

logger = logging.getLogger(__name__)

_DEFAULT_WEIXIN_API_BASE = "https://ilinkai.weixin.qq.com"
_DEFAULT_WEIXIN_CDN_BASE = "https://novac2c.cdn.weixin.qq.com/c2c"
_CHANNEL_VERSION = "1.0.2"
_SESSION_EXPIRED_ERRCODE = -14


# =============================================================================
# Zero-Dependency Pure-Python AES-128-ECB + PKCS7 (with optional cryptography fast-path)
# =============================================================================

_AES_SBOX = (
    0x63, 0x7C, 0x77, 0x7B, 0xF2, 0x6B, 0x6F, 0xC5, 0x30, 0x01, 0x67, 0x2B, 0xFE, 0xD7, 0xAB, 0x76,
    0xCA, 0x82, 0xC9, 0x7D, 0xFA, 0x59, 0x47, 0xF0, 0xAD, 0xD4, 0xA2, 0xAF, 0x9C, 0xA4, 0x72, 0xC0,
    0xB7, 0xFD, 0x93, 0x26, 0x36, 0x3F, 0xF7, 0xCC, 0x34, 0xA5, 0xE5, 0xF1, 0x71, 0xD8, 0x31, 0x15,
    0x04, 0xC7, 0x23, 0xC3, 0x18, 0x96, 0x05, 0x9A, 0x07, 0x12, 0x80, 0xE2, 0xEB, 0x27, 0xB2, 0x75,
    0x09, 0x83, 0x2C, 0x1A, 0x1B, 0x6E, 0x5A, 0xA0, 0x52, 0x3B, 0xD6, 0xB3, 0x29, 0xE3, 0x2F, 0x84,
    0x53, 0xD1, 0x00, 0xED, 0x20, 0xFC, 0xB1, 0x5B, 0x6A, 0xCB, 0xBE, 0x39, 0x4A, 0x4C, 0x58, 0xCF,
    0xD0, 0xEF, 0xAA, 0xFB, 0x43, 0x4D, 0x33, 0x85, 0x45, 0xF9, 0x02, 0x7F, 0x50, 0x3C, 0x9F, 0xA8,
    0x51, 0xA3, 0x40, 0x8F, 0x92, 0x9D, 0x38, 0xF5, 0xBC, 0xB6, 0xDA, 0x21, 0x10, 0xFF, 0xF3, 0xD2,
    0xCD, 0x0C, 0x13, 0xEC, 0x5F, 0x97, 0x44, 0x17, 0xC4, 0xA7, 0x7E, 0x3D, 0x64, 0x5D, 0x19, 0x73,
    0x60, 0x81, 0x4F, 0xDC, 0x22, 0x2A, 0x90, 0x88, 0x46, 0xEE, 0xB8, 0x14, 0xDE, 0x5E, 0x0B, 0xDB,
    0xE0, 0x32, 0x3A, 0x0A, 0x49, 0x06, 0x24, 0x5C, 0xC2, 0xD3, 0xAC, 0x62, 0x91, 0x95, 0xE4, 0x79,
    0xE7, 0xC8, 0x37, 0x6D, 0x8D, 0xD5, 0x4E, 0xA9, 0x6C, 0x56, 0xF4, 0xEA, 0x65, 0x7A, 0xAE, 0x08,
    0xBA, 0x78, 0x25, 0x2E, 0x1C, 0xA6, 0xB4, 0xC6, 0xE8, 0xDD, 0x74, 0x1F, 0x4B, 0xBD, 0x8B, 0x8A,
    0x70, 0x3E, 0xB5, 0x66, 0x48, 0x03, 0xF6, 0x0E, 0x61, 0x35, 0x57, 0xB9, 0x86, 0xC1, 0x1D, 0x9E,
    0xE1, 0xF8, 0x98, 0x11, 0x69, 0xD9, 0x8E, 0x94, 0x9B, 0x1E, 0x87, 0xE9, 0xCE, 0x55, 0x28, 0xDF,
    0x8C, 0xA1, 0x89, 0x0D, 0xBF, 0xE6, 0x42, 0x68, 0x41, 0x99, 0x2D, 0x0F, 0xB0, 0x54, 0xBB, 0x16,
)
_AES_INV_SBOX = tuple(_AES_SBOX.index(i) for i in range(256))
_AES_RCON = (0x01, 0x02, 0x04, 0x08, 0x10, 0x20, 0x40, 0x80, 0x1B, 0x36)


def _xtime(a: int) -> int:
    return ((a << 1) ^ 0x1B) & 0xFF if (a & 0x80) else (a << 1)


def _gmul(a: int, b: int) -> int:
    res = 0
    for _ in range(8):
        if b & 1:
            res ^= a
        hi = a & 0x80
        a = (a << 1) & 0xFF
        if hi:
            a ^= 0x1B
        b >>= 1
    return res


def _aes128_expand_key(key: bytes) -> list[list[int]]:
    if len(key) != 16:
        raise ValueError(f"AES-128 密钥必须为 16 字节，收到 {len(key)} 字节")
    w: list[list[int]] = [list(key[i : i + 4]) for i in range(0, 16, 4)]
    for i in range(4, 44):
        temp = list(w[i - 1])
        if i % 4 == 0:
            temp = [_AES_SBOX[temp[1]] ^ _AES_RCON[i // 4 - 1], _AES_SBOX[temp[2]], _AES_SBOX[temp[3]], _AES_SBOX[temp[0]]]
        w.append([w[i - 4][j] ^ temp[j] for j in range(4)])
    return [[w[r * 4 + c][row] for c in range(4) for row in range(4)] for r in range(11)]


def _aes128_encrypt_block(block: bytes, rks: list[list[int]]) -> bytes:
    s = [block[i] ^ rks[0][i] for i in range(16)]
    for r in range(1, 10):
        s = [_AES_SBOX[b] for b in s]
        # ShiftRows (column-major state)
        s = [
            s[0], s[5], s[10], s[15],
            s[4], s[9], s[14], s[3],
            s[8], s[13], s[2], s[7],
            s[12], s[1], s[6], s[11],
        ]
        # MixColumns
        out = [0] * 16
        for c in range(0, 16, 4):
            a0, a1, a2, a3 = s[c], s[c + 1], s[c + 2], s[c + 3]
            t = a0 ^ a1 ^ a2 ^ a3
            out[c] = a0 ^ t ^ _xtime(a0 ^ a1)
            out[c + 1] = a1 ^ t ^ _xtime(a1 ^ a2)
            out[c + 2] = a2 ^ t ^ _xtime(a2 ^ a3)
            out[c + 3] = a3 ^ t ^ _xtime(a3 ^ a0)
        rk = rks[r]
        s = [out[i] ^ rk[i] for i in range(16)]
    s = [_AES_SBOX[b] for b in s]
    s = [
        s[0], s[5], s[10], s[15],
        s[4], s[9], s[14], s[3],
        s[8], s[13], s[2], s[7],
        s[12], s[1], s[6], s[11],
    ]
    rk = rks[10]
    return bytes(s[i] ^ rk[i] for i in range(16))


def _aes128_decrypt_block(block: bytes, rks: list[list[int]]) -> bytes:
    s = [block[i] ^ rks[10][i] for i in range(16)]
    for r in range(9, 0, -1):
        # InvShiftRows
        s = [
            s[0], s[13], s[10], s[7],
            s[4], s[1], s[14], s[11],
            s[8], s[5], s[2], s[15],
            s[12], s[9], s[6], s[3],
        ]
        s = [_AES_INV_SBOX[b] for b in s]
        rk = rks[r]
        s = [s[i] ^ rk[i] for i in range(16)]
        # InvMixColumns
        out = [0] * 16
        for c in range(0, 16, 4):
            a0, a1, a2, a3 = s[c], s[c + 1], s[c + 2], s[c + 3]
            out[c] = _gmul(a0, 14) ^ _gmul(a1, 11) ^ _gmul(a2, 13) ^ _gmul(a3, 9)
            out[c + 1] = _gmul(a0, 9) ^ _gmul(a1, 14) ^ _gmul(a2, 11) ^ _gmul(a3, 13)
            out[c + 2] = _gmul(a0, 13) ^ _gmul(a1, 9) ^ _gmul(a2, 14) ^ _gmul(a3, 11)
            out[c + 3] = _gmul(a0, 11) ^ _gmul(a1, 13) ^ _gmul(a2, 9) ^ _gmul(a3, 14)
        s = out
    s = [
        s[0], s[13], s[10], s[7],
        s[4], s[1], s[14], s[11],
        s[8], s[5], s[2], s[15],
        s[12], s[9], s[6], s[3],
    ]
    s = [_AES_INV_SBOX[b] for b in s]
    rk = rks[0]
    return bytes(s[i] ^ rk[i] for i in range(16))


def aes128_ecb_encrypt(data: bytes, key: bytes) -> bytes:
    """Encrypt bytes with AES-128-ECB + PKCS7 padding."""
    pad_len = 16 - (len(data) % 16)
    padded = data + bytes([pad_len] * pad_len)
    try:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes  # type: ignore[import-not-found]

        enc = Cipher(algorithms.AES(key), modes.ECB()).encryptor()
        return enc.update(padded) + enc.finalize()
    except Exception:
        pass
    rks = _aes128_expand_key(key)
    return b"".join(_aes128_encrypt_block(padded[i : i + 16], rks) for i in range(0, len(padded), 16))


def aes128_ecb_decrypt(ciphertext: bytes, key: bytes) -> bytes:
    """Decrypt AES-128-ECB + PKCS7 padded bytes."""
    if not ciphertext or (len(ciphertext) % 16) != 0:
        raise ValueError("密文长度不是 16 字节的整数倍")
    try:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes  # type: ignore[import-not-found]

        dec = Cipher(algorithms.AES(key), modes.ECB()).decryptor()
        padded = dec.update(ciphertext) + dec.finalize()
    except Exception:
        rks = _aes128_expand_key(key)
        padded = b"".join(_aes128_decrypt_block(ciphertext[i : i + 16], rks) for i in range(0, len(ciphertext), 16))
    pad_len = padded[-1]
    if 1 <= pad_len <= 16 and padded[-pad_len:] == bytes([pad_len] * pad_len):
        return padded[:-pad_len]
    return padded


def parse_ilink_aes_key(raw_key: str) -> bytes | None:
    """Parse iLink AES-128 key which may be raw 32-hex, base64(16 bytes), or base64(32-char hex ASCII)."""
    val = (raw_key or "").strip()
    if not val:
        return None
    if len(val) == 32:
        try:
            return bytes.fromhex(val)
        except ValueError:
            pass
    try:
        decoded = base64.b64decode(val)
        if len(decoded) == 16:
            return decoded
        if len(decoded) == 32:
            try:
                return bytes.fromhex(decoded.decode("ascii"))
            except Exception:
                pass
    except Exception:
        pass
    return None


# =============================================================================
# Zero-Dependency ISO/IEC 18004 QR Code SVG Generator (Versions 1..10, Byte Mode, EC Level L)
# =============================================================================

_QR_EC_L_PARAMS: dict[int, tuple[int, int, int, int, int]] = {
    # version: (total_codewords, ec_per_block, g1_blocks, g1_data_cw, g2_blocks)
    # g2_data_cw = g1_data_cw + 1
    1: (26, 7, 1, 19, 0),
    2: (44, 10, 1, 34, 0),
    3: (70, 15, 1, 55, 0),
    4: (100, 20, 1, 80, 0),
    5: (134, 26, 1, 108, 0),
    6: (172, 18, 2, 68, 0),
    7: (196, 20, 2, 78, 0),
    8: (242, 24, 2, 97, 0),
    9: (292, 30, 2, 116, 0),
    10: (346, 18, 2, 68, 2),
}

_QR_ALIGN_POS: dict[int, tuple[int, ...]] = {
    1: (),
    2: (6, 18),
    3: (6, 22),
    4: (6, 26),
    5: (6, 30),
    6: (6, 34),
    7: (6, 22, 38),
    8: (6, 24, 42),
    9: (6, 26, 46),
    10: (6, 28, 50),
}

# Precomputed 18-bit version info strings for versions 7..10 (ISO/IEC 18004 Annex D)
_QR_VERSION_INFO: dict[int, int] = {
    7: 0x07C94,
    8: 0x085BC,
    9: 0x09A99,
    10: 0x0A4D3,
}

# Precomputed 15-bit format info for EC Level L (01) and masks 0..7
_QR_FORMAT_L: tuple[int, ...] = (
    0x77C4, 0x72F3, 0x7DAA, 0x789D, 0x662F, 0x6318, 0x6C41, 0x6976,
)


def _qr_gf_mul(a: int, b: int) -> int:
    res = 0
    for _ in range(8):
        if b & 1:
            res ^= a
        hi = a & 0x80
        a = (a << 1) & 0xFF
        if hi:
            a ^= 0x1D
        b >>= 1
    return res


def _qr_rs_remainder(data: bytes, ec_len: int) -> bytes:
    gen = [1]
    p = 1
    for _ in range(ec_len):
        nxt = [0] * (len(gen) + 1)
        for i, c in enumerate(gen):
            nxt[i] ^= c
            nxt[i + 1] ^= _qr_gf_mul(c, p)
        gen = nxt
        p = _qr_gf_mul(p, 2)
    rem = [0] * ec_len
    for b in data:
        factor = b ^ rem.pop(0)
        rem.append(0)
        for i in range(ec_len):
            rem[i] ^= _qr_gf_mul(gen[i + 1], factor)
    return bytes(rem)


def encode_qr_matrix(text: str) -> list[list[bool]]:
    """Encode UTF-8 text into a standard QR code boolean matrix (True = dark module)."""
    payload = text.encode("utf-8")
    ver = 0
    for v in range(1, 11):
        _, _, g1_b, g1_d, g2_b = _QR_EC_L_PARAMS[v]
        data_cap = g1_b * g1_d + g2_b * (g1_d + 1)
        len_bits = 8 if v <= 9 else 16
        if (4 + len_bits + len(payload) * 8) <= data_cap * 8:
            ver = v
            break
    if ver == 0:
        raise ValueError("二维码内容过长")

    _, ec_len, g1_b, g1_d, g2_b = _QR_EC_L_PARAMS[ver]
    data_cap = g1_b * g1_d + g2_b * (g1_d + 1)
    len_bits = 8 if ver <= 9 else 16

    bits: list[int] = []

    def _push(val: int, nbits: int) -> None:
        for i in range(nbits - 1, -1, -1):
            bits.append((val >> i) & 1)

    _push(0b0100, 4)  # Byte mode
    _push(len(payload), len_bits)
    for b in payload:
        _push(b, 8)
    term = min(4, data_cap * 8 - len(bits))
    _push(0, term)
    while len(bits) % 8 != 0:
        bits.append(0)
    data_cw = bytearray()
    for i in range(0, len(bits), 8):
        byte_val = 0
        for j in range(8):
            byte_val = (byte_val << 1) | bits[i + j]
        data_cw.append(byte_val)
    pad_bytes = (0xEC, 0x11)
    pi = 0
    while len(data_cw) < data_cap:
        data_cw.append(pad_bytes[pi % 2])
        pi += 1

    # Split into RS blocks and interleave
    blocks: list[bytes] = []
    ec_blocks: list[bytes] = []
    pos = 0
    for _ in range(g1_b):
        blk = bytes(data_cw[pos : pos + g1_d])
        pos += g1_d
        blocks.append(blk)
        ec_blocks.append(_qr_rs_remainder(blk, ec_len))
    for _ in range(g2_b):
        blk = bytes(data_cw[pos : pos + g1_d + 1])
        pos += g1_d + 1
        blocks.append(blk)
        ec_blocks.append(_qr_rs_remainder(blk, ec_len))

    stream: list[int] = []
    max_d = max(len(b) for b in blocks)
    for i in range(max_d):
        for blk in blocks:
            if i < len(blk):
                stream.append(blk[i])
    for i in range(ec_len):
        for ec in ec_blocks:
            stream.append(ec[i])

    size = ver * 4 + 17
    modules: list[list[bool | None]] = [[None] * size for _ in range(size)]
    is_func: list[list[bool]] = [[False] * size for _ in range(size)]

    def _set_func(r: int, c: int, dark: bool) -> None:
        if 0 <= r < size and 0 <= c < size:
            modules[r][c] = dark
            is_func[r][c] = True

    def _draw_finder(r0: int, c0: int) -> None:
        for dr in range(-1, 8):
            for dc in range(-1, 8):
                r, c = r0 + dr, c0 + dc
                if 0 <= r < size and 0 <= c < size:
                    in_outer = 0 <= dr <= 6 and 0 <= dc <= 6
                    dark = in_outer and (
                        dr in (0, 6) or dc in (0, 6) or (2 <= dr <= 4 and 2 <= dc <= 4)
                    )
                    _set_func(r, c, dark)

    _draw_finder(0, 0)
    _draw_finder(0, size - 7)
    _draw_finder(size - 7, 0)

    # Timing patterns
    for i in range(8, size - 8):
        _set_func(6, i, (i % 2) == 0)
        _set_func(i, 6, (i % 2) == 0)

    # Alignment patterns
    centers = _QR_ALIGN_POS[ver]
    for r0 in centers:
        for c0 in centers:
            if (r0 <= 8 and c0 <= 8) or (r0 <= 8 and c0 >= size - 9) or (r0 >= size - 9 and c0 <= 8):
                continue
            for dr in range(-2, 3):
                for dc in range(-2, 3):
                    dark = max(abs(dr), abs(dc)) != 1
                    _set_func(r0 + dr, c0 + dc, dark)

    # Reserve format info + dark module
    for i in range(9):
        if not is_func[8][i]:
            _set_func(8, i, False)
        if not is_func[i][8]:
            _set_func(i, 8, False)
    for i in range(8):
        _set_func(8, size - 1 - i, False)
        _set_func(size - 1 - i, 8, False)
    _set_func(size - 8, 8, True)

    # Version info for v >= 7
    if ver >= 7:
        vbits = _QR_VERSION_INFO[ver]
        for i in range(18):
            bit = bool((vbits >> i) & 1)
            a = size - 11 + (i % 3)
            b = i // 3
            _set_func(a, b, bit)
            _set_func(b, a, bit)

    # Place data bits (zigzag right-to-left, skipping col 6)
    bit_idx = 0
    total_bits = len(stream) * 8
    up = True
    c = size - 1
    while c > 0:
        if c == 6:
            c -= 1
        rows = range(size - 1, -1, -1) if up else range(size)
        for r in rows:
            for dc in (0, -1):
                col = c + dc
                if not is_func[r][col]:
                    if bit_idx < total_bits:
                        bval = bool((stream[bit_idx >> 3] >> (7 - (bit_idx & 7))) & 1)
                        bit_idx += 1
                    else:
                        bval = False
                    modules[r][col] = bval
        up = not up
        c -= 2

    # Apply mask 0: (r + c) % 2 == 0 and write format info for EC-L mask 0
    mask_id = 0
    final: list[list[bool]] = [[False] * size for _ in range(size)]
    for r in range(size):
        for col in range(size):
            val = bool(modules[r][col])
            if not is_func[r][col] and ((r + col) % 2 == 0):
                val = not val
            final[r][col] = val

    fmt = _QR_FORMAT_L[mask_id]
    # Top-left horizontal & vertical
    coords1 = [(8, 0), (8, 1), (8, 2), (8, 3), (8, 4), (8, 5), (8, 7), (8, 8), (7, 8), (5, 8), (4, 8), (3, 8), (2, 8), (1, 8), (0, 8)]
    coords2 = [(size - 1, 8), (size - 2, 8), (size - 3, 8), (size - 4, 8), (size - 5, 8), (size - 6, 8), (size - 7, 8), (8, size - 8), (8, size - 7), (8, size - 6), (8, size - 5), (8, size - 4), (8, size - 3), (8, size - 2), (8, size - 1)]
    for i in range(15):
        bit = bool((fmt >> (14 - i)) & 1)
        r1, c1 = coords1[i]
        r2, c2 = coords2[i]
        final[r1][c1] = bit
        final[r2][c2] = bit
    final[size - 8][8] = True
    return final


def render_qr_svg_data_uri(text: str, *, quiet_zone: int = 4) -> str:
    """Render text into a crisp inline SVG data URI (`data:image/svg+xml;base64,...`)."""
    matrix = encode_qr_matrix(text)
    n = len(matrix)
    dim = n + quiet_zone * 2
    rects: list[str] = []
    for r, row in enumerate(matrix):
        for c, dark in enumerate(row):
            if dark:
                rects.append(f"M{c + quiet_zone} {r + quiet_zone}h1v1h-1z")
    path_d = "".join(rects)
    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {dim} {dim}" shape-rendering="crispEdges">'
        f'<rect width="{dim}" height="{dim}" fill="#ffffff"/>'
        f'<path d="{path_d}" fill="#000000"/>'
        f"</svg>"
    )
    b64 = base64.b64encode(svg.encode("utf-8")).decode("ascii")
    return f"data:image/svg+xml;base64,{b64}"


# =============================================================================
# WeChat iLink Protocol Helpers
# =============================================================================

def normalize_weixin_api_base(raw_url: str) -> str:
    val = (raw_url or "").strip().rstrip("/")
    if not val or val.lower().startswith(("ws://", "wss://")):
        return _DEFAULT_WEIXIN_API_BASE
    if not val.lower().startswith(("http://", "https://")):
        return f"https://{val}"
    return val


def resolve_weixin_proxy(config: PlatformConfig, default_proxy: str = "") -> str:
    extra = config.extra if isinstance(config.extra, dict) else {}
    explicit = str(extra.get("proxy") or "").strip()
    if explicit:
        return explicit
    # Domestic Tencent service defaults to direct connection unless explicitly enabled
    use_agy = extra.get("use_agy_proxy", False)
    if use_agy is True or str(use_agy).strip().lower() in ("1", "true", "yes", "on"):
        return (default_proxy or "").strip()
    return ""


def _aiohttp_proxy_arg(proxy_url: str) -> str | None:
    p = (proxy_url or "").strip()
    if p and p.lower().startswith(("http://", "https://")):
        return p
    return None


def _random_wechat_uin() -> str:
    val = str(random.randint(1, 0xFFFFFFFF))
    return base64.b64encode(val.encode("ascii")).decode("ascii")


def _ilink_post_headers(token: str) -> dict[str, str]:
    headers = {
        "Content-Type": "application/json",
        "AuthorizationType": "ilink_bot_token",
        "X-WECHAT-UIN": _random_wechat_uin(),
    }
    clean = (token or "").strip()
    if clean:
        headers["Authorization"] = f"Bearer {clean}"
    return headers


async def fetch_weixin_qr_code(
    *,
    api_base: str = "",
    bot_type: str = "3",
    proxy: str = "",
    timeout_seconds: float = 10.0,
) -> dict[str, Any]:
    """Request a fresh official WeChat iLink ClawBot login QR code."""
    base = normalize_weixin_api_base(api_base)
    url = f"{base}/ilink/bot/get_bot_qrcode?bot_type={urllib.parse.quote(str(bot_type or '3'))}"
    eff_proxy = _aiohttp_proxy_arg(proxy)
    timeout = aiohttp.ClientTimeout(total=max(3.0, float(timeout_seconds)))
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, proxy=eff_proxy) as resp:
                data = await resp.json(content_type=None)
                if resp.status == 200 and isinstance(data, dict) and data.get("qrcode"):
                    qrcode_id = str(data.get("qrcode") or "").strip()
                    qr_content = str(data.get("qrcode_img_content") or "").strip()
                    svg_uri = ""
                    if qr_content.startswith("data:image/"):
                        svg_uri = qr_content
                    elif qr_content:
                        try:
                            svg_uri = render_qr_svg_data_uri(qr_content)
                        except Exception as exc:
                            logger.warning("生成微信登录二维码 SVG 失败: %s", exc)
                    return {
                        "ok": True,
                        "api_base": base,
                        "qrcode": qrcode_id,
                        "qrcode_url": qr_content,
                        "qrcode_svg_data_uri": svg_uri,
                    }
                return {
                    "ok": False,
                    "api_base": base,
                    "error": f"获取微信登录二维码失败 (HTTP {resp.status}): {data}",
                }
    except Exception as exc:
        return {"ok": False, "api_base": base, "error": f"请求微信 iLink 接口失败: {exc}"}


async def poll_weixin_qr_status(
    qrcode: str,
    *,
    api_base: str = "",
    proxy: str = "",
    timeout_seconds: float = 10.0,
) -> dict[str, Any]:
    """Poll official WeChat iLink QR code scan status (long-poll safe: timeout -> 'wait')."""
    qr = (qrcode or "").strip()
    base = normalize_weixin_api_base(api_base)
    if not qr:
        return {"ok": False, "error": "缺少 qrcode 参数"}
    url = f"{base}/ilink/bot/get_qrcode_status?qrcode={urllib.parse.quote(qr)}"
    eff_proxy = _aiohttp_proxy_arg(proxy)
    timeout = aiohttp.ClientTimeout(total=max(3.0, float(timeout_seconds)))
    headers = {"iLink-App-ClientVersion": "1"}
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, headers=headers, proxy=eff_proxy) as resp:
                data = await resp.json(content_type=None)
                if resp.status == 200 and isinstance(data, dict):
                    status = str(data.get("status") or "wait").strip().lower()
                    return {
                        "ok": True,
                        "status": status,
                        "bot_token": str(data.get("bot_token") or data.get("ilink_bot_token") or "").strip(),
                        "baseurl": str(data.get("baseurl") or "").strip(),
                        "ilink_bot_id": str(data.get("ilink_bot_id") or "").strip(),
                        "ilink_user_id": str(data.get("ilink_user_id") or "").strip(),
                    }
                return {"ok": False, "error": f"查询扫码状态失败 (HTTP {resp.status}): {data}"}
    except asyncio.TimeoutError:
        # get_qrcode_status holds the HTTP connection open as long-poll while waiting for scan
        return {"ok": True, "status": "wait"}
    except Exception as exc:
        return {"ok": False, "error": f"查询微信扫码状态失败: {exc}"}


async def probe_weixin_ilink(
    token: str,
    *,
    api_base: str = "",
    proxy: str = "",
    timeout_seconds: float = 5.0,
) -> dict[str, Any]:
    """Verify WeChat iLink bot_token reachability & validity via a short getupdates probe."""
    clean_tok = (token or "").strip()
    base = normalize_weixin_api_base(api_base)
    if not clean_tok:
        return {"ok": False, "error": "尚未扫码获取微信 Bot Token，请先点击「扫码登录个人微信」"}

    url = f"{base}/ilink/bot/getupdates"
    eff_proxy = _aiohttp_proxy_arg(proxy)
    payload = {"get_updates_buf": "", "base_info": {"channel_version": _CHANNEL_VERSION}}
    timeout = aiohttp.ClientTimeout(total=max(2.5, min(8.0, float(timeout_seconds))))
    t0 = time.monotonic()
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(url, json=payload, headers=_ilink_post_headers(clean_tok), proxy=eff_proxy) as resp:
                latency_ms = round((time.monotonic() - t0) * 1000.0, 1)
                data = await resp.json(content_type=None)
                if resp.status == 200 and isinstance(data, dict):
                    errcode = int(data.get("errcode") or data.get("ret") or 0)
                    if errcode == _SESSION_EXPIRED_ERRCODE:
                        return {
                            "ok": False,
                            "latency_ms": latency_ms,
                            "api_base": base,
                            "error": "微信登录凭证已过期 (errcode=-14)，请点击「重新扫码登录」",
                        }
                    if errcode != 0:
                        return {
                            "ok": False,
                            "latency_ms": latency_ms,
                            "api_base": base,
                            "error": f"微信 iLink 返回错误 (errcode={errcode}): {data.get('errmsg') or data}",
                        }
                    return {"ok": True, "latency_ms": latency_ms, "api_base": base}
                return {
                    "ok": False,
                    "latency_ms": latency_ms,
                    "api_base": base,
                    "error": f"微信 iLink 接口返回 HTTP {resp.status}: {data}",
                }
    except asyncio.TimeoutError:
        # Long-polling held the connection open cleanly without rejecting credentials
        return {
            "ok": True,
            "latency_ms": round((time.monotonic() - t0) * 1000.0, 1),
            "api_base": base,
            "note": "长轮询连接已建立并保持监听",
        }
    except Exception as exc:
        return {
            "ok": False,
            "latency_ms": round((time.monotonic() - t0) * 1000.0, 1),
            "api_base": base,
            "error": f"连接微信 iLink 接口失败: {exc}",
        }


# =============================================================================
# WeixinILinkAdapter
# =============================================================================

class WeixinILinkAdapter(PlatformAdapter):
    """Official Tencent WeChat personal account adapter via iLink / ClawBot HTTP long-polling.

    - Official compliant channel (`https://ilinkai.weixin.qq.com`), no public IP / webhook needed.
    - Supports direct private chat (text, voice transcript, images, files, quoted replies).
    - Reply-driven via `context_token` echoed from incoming messages.
    """

    def __init__(
        self,
        config: PlatformConfig,
        *,
        message_lookup: MessageLookupFn | None = None,
        ws_connect: WsConnectFn | None = None,
        default_proxy: str = "",
    ) -> None:
        super().__init__(config, message_lookup=message_lookup, ws_connect=ws_connect)
        self.platform_name = "weixin_ilink"
        self.self_id: str = str(config.self_id or "")
        self.bot_username: str = str((config.extra or {}).get("ilink_user_id") or "")
        self.connected: bool = False
        self._default_proxy: str = (default_proxy or "").strip()
        self._stop_event = asyncio.Event()
        self._session: aiohttp.ClientSession | None = None
        self._updates_buf: str = ""
        self._context_tokens: dict[str, str] = {}
        self._seen_msg_ids: dict[str, float] = {}
        self._media_cache_dir = Path("data") / "media_cache" / "weixin_ilink"

    @property
    def api_base(self) -> str:
        return normalize_weixin_api_base(self.config.ws_url)

    @property
    def cdn_base(self) -> str:
        extra = self.config.extra if isinstance(self.config.extra, dict) else {}
        val = str(extra.get("cdn_base_url") or "").strip().rstrip("/")
        return val or _DEFAULT_WEIXIN_CDN_BASE

    @property
    def bot_token(self) -> str:
        return (self.config.access_token or "").strip()

    @property
    def effective_proxy(self) -> str | None:
        return _aiohttp_proxy_arg(resolve_weixin_proxy(self.config, self._default_proxy))

    def _remember_context_token(self, user_id: str, token: str) -> None:
        uid = (user_id or "").strip()
        tok = (token or "").strip()
        if uid and tok:
            self._context_tokens[uid] = tok
            if len(self._context_tokens) > 2000:
                oldest = next(iter(self._context_tokens))
                self._context_tokens.pop(oldest, None)

    def _is_duplicate_msg(self, msg_id: str) -> bool:
        mid = (msg_id or "").strip()
        if not mid:
            return False
        now = time.monotonic()
        if mid in self._seen_msg_ids:
            return True
        self._seen_msg_ids[mid] = now
        if len(self._seen_msg_ids) > 2000:
            cutoff = now - 600.0
            for k in [k for k, ts in self._seen_msg_ids.items() if ts < cutoff]:
                self._seen_msg_ids.pop(k, None)
        return False

    async def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def _post_json(self, path: str, payload: dict[str, Any], *, timeout_seconds: float = 15.0) -> dict[str, Any]:
        if not self.bot_token:
            raise RuntimeError(f"[{self.adapter_id}] 未配置微信 iLink bot_token，请先在 WebUI 扫码登录")
        session = await self._ensure_session()
        url = f"{self.api_base}{path}"
        timeout = aiohttp.ClientTimeout(total=max(5.0, float(timeout_seconds)))
        async with session.post(
            url,
            json=payload,
            headers=_ilink_post_headers(self.bot_token),
            proxy=self.effective_proxy,
            timeout=timeout,
        ) as resp:
            data = await resp.json(content_type=None)
            if resp.status != 200:
                raise RuntimeError(f"微信 iLink HTTP {resp.status}: {data}")
            if not isinstance(data, dict):
                raise RuntimeError(f"微信 iLink 返回非 JSON 对象: {data}")
            return data

    def _parse_inbound_message(self, raw: dict[str, Any]) -> IncomingMessage | None:
        if not isinstance(raw, dict):
            return None
        # message_type: 1 = user message, 2 = bot message
        msg_type = int(raw.get("message_type") or 1)
        if msg_type == 2:
            return None
        from_uid = str(raw.get("from_user_id") or "").strip()
        if not from_uid or (self.self_id and from_uid == self.self_id):
            return None

        msg_id = str(raw.get("message_id") or raw.get("seq") or raw.get("client_id") or "").strip()
        if msg_id and self._is_duplicate_msg(msg_id):
            return None

        ctx_token = str(raw.get("context_token") or "").strip()
        if ctx_token:
            self._remember_context_token(from_uid, ctx_token)

        if not self.is_channel_allowed("private", from_uid, from_uid):
            return None

        items = raw.get("item_list")
        if not isinstance(items, list):
            items = []

        text_parts: list[str] = []
        attachments: list[MediaAttachment] = []
        reply_to_text: str | None = None

        for it in items:
            if not isinstance(it, dict):
                continue
            itype = int(it.get("type") or 0)
            if itype == 1 or isinstance(it.get("text_item"), dict):
                ti = it.get("text_item") if isinstance(it.get("text_item"), dict) else {}
                txt = str(ti.get("text") or "").strip()
                if txt:
                    text_parts.append(txt)
                ref = it.get("ref_msg")
                if isinstance(ref, dict):
                    ref_title = str(ref.get("title") or "").strip()
                    ref_inner = ref.get("message_item") if isinstance(ref.get("message_item"), dict) else {}
                    ref_ti = ref_inner.get("text_item") if isinstance(ref_inner.get("text_item"), dict) else {}
                    ref_body = str(ref_ti.get("text") or ref_title).strip()
                    if ref_body:
                        reply_to_text = ref_body
            elif itype == 2 or isinstance(it.get("image_item"), dict):
                im = it.get("image_item") if isinstance(it.get("image_item"), dict) else {}
                media = im.get("media") if isinstance(im.get("media"), dict) else {}
                enc_param = str(media.get("encrypt_query_param") or "").strip()
                full_url = str(media.get("full_url") or "").strip()
                aes_key = str(im.get("aeskey") or media.get("aes_key") or "").strip()
                if enc_param or full_url:
                    ref_payload = json.dumps(
                        {"encrypt_query_param": enc_param, "full_url": full_url, "aes_key": aes_key},
                        ensure_ascii=False,
                    )
                    key_seed = enc_param or full_url
                    attachments.append(
                        MediaAttachment(
                            kind="image",
                            file_id=ref_payload,
                            file_name=f"wx_{hashlib.md5(key_seed.encode('utf-8')).hexdigest()[:12]}.jpg",
                            source="message",
                        )
                    )
                text_parts.append("[图片]")
            elif itype == 3 or isinstance(it.get("voice_item"), dict):
                vi = it.get("voice_item") if isinstance(it.get("voice_item"), dict) else {}
                transcript = str(vi.get("text") or "").strip()
                if transcript:
                    text_parts.append(f"[语音转文字] {transcript}")
                else:
                    text_parts.append("[语音]")
            elif itype == 4 or isinstance(it.get("file_item"), dict):
                fi = it.get("file_item") if isinstance(it.get("file_item"), dict) else {}
                fname = str(fi.get("file_name") or "未命名文件").strip()
                text_parts.append(f"[文件: {fname}]")
            elif itype == 5 or isinstance(it.get("video_item"), dict):
                text_parts.append("[视频]")

        combined_text = "\n".join(p for p in text_parts if p).strip()
        if not combined_text and not attachments:
            return None

        ts_raw = raw.get("create_time_ms") or raw.get("create_time") or 0
        try:
            ts_val = float(ts_raw)
            if ts_val > 1e12:
                ts_val /= 1000.0
            if ts_val <= 0:
                ts_val = time.time()
        except (TypeError, ValueError):
            ts_val = time.time()

        sender_name = str(raw.get("from_user_name") or raw.get("sender_name") or "").strip()
        if not sender_name:
            sender_name = from_uid.split("@")[0] if "@" in from_uid else from_uid

        return IncomingMessage(
            adapter_id=self.adapter_id,
            platform=self.platform_name,
            self_id=self.self_id,
            scene="private",
            channel_id=from_uid,
            message_id=msg_id or uuid.uuid4().hex[:12],
            user_id=from_uid,
            sender_name=sender_name,
            text=combined_text,
            timestamp=ts_val,
            raw_segments=[Segment(type="text", data={"text": combined_text})] if combined_text else [],
            reply_to_text=reply_to_text,
            raw_event=dict(raw),
            attachments=attachments,
        )

    async def start(self, on_message: OnMessageFn) -> None:
        self._stop_event.clear()
        backoff = max(1.0, float(self.config.reconnect_initial_seconds or 2.0))
        max_backoff = max(backoff, float(self.config.reconnect_max_seconds or 60.0))

        if not self.bot_token:
            logger.warning("[%s] 个人微信 (iLink) 尚未扫码登录（缺少 bot_token），请在 WebUI「平台」页扫码授权", self.adapter_id)
            await self._stop_event.wait()
            return

        logger.info("[%s] 启动个人微信 (官方 iLink) 长轮询监听: %s (self_id=%s)", self.adapter_id, self.api_base, self.self_id or "auto")
        consecutive_errors = 0

        while not self._stop_event.is_set():
            try:
                payload = {
                    "get_updates_buf": self._updates_buf,
                    "base_info": {"channel_version": _CHANNEL_VERSION},
                }
                data = await self._post_json("/ilink/bot/getupdates", payload, timeout_seconds=45.0)
                errcode = int(data.get("errcode") or data.get("ret") or 0)
                if errcode == _SESSION_EXPIRED_ERRCODE:
                    self.connected = False
                    logger.error(
                        "[%s] 微信 iLink 登录凭证已过期 (errcode=-14)，请在 WebUI「全局配置 → 平台」重新扫码登录",
                        self.adapter_id,
                    )
                    try:
                        await asyncio.wait_for(self._stop_event.wait(), timeout=60.0)
                    except asyncio.TimeoutError:
                        pass
                    continue
                if errcode != 0:
                    raise RuntimeError(f"getupdates errcode={errcode}: {data.get('errmsg') or data}")

                if not self.connected:
                    self.connected = True
                    logger.info("[%s] 个人微信 (官方 iLink) 已上线并进入实时长轮询", self.adapter_id)
                consecutive_errors = 0

                new_buf = data.get("get_updates_buf")
                if isinstance(new_buf, str) and new_buf:
                    self._updates_buf = new_buf

                msgs = data.get("msgs")
                if isinstance(msgs, list):
                    for raw_msg in msgs:
                        parsed = self._parse_inbound_message(raw_msg)
                        if parsed is not None:
                            await on_message(parsed)

            except asyncio.CancelledError:
                raise
            except asyncio.TimeoutError:
                # Normal HTTP long-poll cycle timeout with no new messages
                if not self.connected:
                    self.connected = True
                    logger.info("[%s] 个人微信 (官方 iLink) 已上线并保持长轮询", self.adapter_id)
                consecutive_errors = 0
            except Exception as exc:
                self.connected = False
                consecutive_errors += 1
                wait_sec = min(max_backoff, backoff * (2 ** min(consecutive_errors - 1, 5)))
                logger.warning("[%s] 微信 iLink 长轮询异常 (%s)，%.1fs 后重试", self.adapter_id, exc, wait_sec)
                try:
                    await asyncio.wait_for(self._stop_event.wait(), timeout=wait_sec)
                except asyncio.TimeoutError:
                    pass

        self.connected = False

    async def stop(self) -> None:
        self._stop_event.set()
        self.connected = False
        if self._session is not None and not self._session.closed:
            with contextlib.suppress(Exception):
                await self._session.close()
        self._session = None

    async def send_text(self, scene: str, channel_id: str, text: str, *, context_token: str = "") -> str | None:
        to_uid = (channel_id or "").strip()
        body = (text or "").strip()
        if not to_uid or not body:
            return None
        ctx_tok = (context_token or "").strip() or self._context_tokens.get(to_uid, "")
        if not ctx_tok:
            logger.warning("[%s] 无法向微信用户 %s 发送消息：缺少 context_token（官方 iLink 仅支持回复已互动会话）", self.adapter_id, to_uid)
            return None
        client_id = f"agy_{uuid.uuid4().hex[:16]}"
        payload = {
            "msg": {
                "from_user_id": "",
                "to_user_id": to_uid,
                "client_id": client_id,
                "message_type": 2,
                "message_state": 2,
                "context_token": ctx_tok,
                "item_list": [
                    {
                        "type": 1,
                        "text_item": {"text": body},
                    }
                ],
            },
            "base_info": {"channel_version": _CHANNEL_VERSION},
        }
        data = await self._post_json(
            "/ilink/bot/sendmessage",
            payload,
            timeout_seconds=max(10.0, float(self.config.rpc_timeout_seconds or 15.0)),
        )
        errcode = int(data.get("errcode") or data.get("ret") or 0)
        if errcode != 0:
            raise RuntimeError(f"微信发送消息失败 (errcode={errcode}): {data.get('errmsg') or data}")
        return client_id

    async def send_reply(
        self,
        target: IncomingMessage,
        text: str,
        *,
        quote_trigger: bool = True,
        at_user: bool = False,
    ) -> str | None:
        ctx_tok = str((target.raw_event or {}).get("context_token") or "").strip()
        return await self.send_text(target.scene, target.channel_id, text, context_token=ctx_tok)

    async def _load_image_bytes(self, image: bytes | str | Path) -> bytes:
        if isinstance(image, (bytes, bytearray)):
            return bytes(image)
        if isinstance(image, Path):
            return image.read_bytes()
        s = str(image or "").strip()
        if s.startswith("base64://"):
            return base64.b64decode(s[9:])
        if s.startswith(("http://", "https://")):
            session = await self._ensure_session()
            async with session.get(s, proxy=self.effective_proxy, timeout=aiohttp.ClientTimeout(total=20.0)) as resp:
                if resp.status != 200:
                    raise RuntimeError(f"下载待发图片失败: HTTP {resp.status}")
                return await resp.read()
        p = Path(s)
        if p.is_file():
            return p.read_bytes()
        raise FileNotFoundError(f"无法读取待发送图片: {s}")

    async def send_image(
        self,
        target: IncomingMessage,
        image: bytes | str | Path,
        *,
        caption: str = "",
        quote_trigger: bool = True,
        at_user: bool = False,
    ) -> str | None:
        to_uid = (target.channel_id or "").strip()
        ctx_tok = str((target.raw_event or {}).get("context_token") or "").strip() or self._context_tokens.get(to_uid, "")
        if not to_uid or not ctx_tok:
            logger.warning("[%s] 无法向微信用户 %s 发送图片：缺少 context_token", self.adapter_id, to_uid)
            return None

        raw_bytes = await self._load_image_bytes(image)
        aes_key = os.urandom(16)
        aes_hex = aes_key.hex()
        encrypted = aes128_ecb_encrypt(raw_bytes, aes_key)
        filekey = os.urandom(16).hex()

        up_req = {
            "filekey": filekey,
            "media_type": 1,
            "to_user_id": to_uid,
            "rawsize": len(raw_bytes),
            "rawfilemd5": hashlib.md5(raw_bytes).hexdigest(),
            "filesize": len(encrypted),
            "no_need_thumb": True,
            "aeskey": aes_hex,
            "base_info": {"channel_version": _CHANNEL_VERSION},
        }
        up_resp = await self._post_json("/ilink/bot/getuploadurl", up_req, timeout_seconds=15.0)
        upload_full_url = str(up_resp.get("upload_full_url") or "").strip()
        upload_param = str(up_resp.get("upload_param") or "").strip()
        if upload_full_url:
            cdn_url = upload_full_url
        elif upload_param:
            cdn_url = (
                f"{self.cdn_base}/upload?encrypted_query_param={urllib.parse.quote(upload_param)}"
                f"&filekey={urllib.parse.quote(filekey)}"
            )
        else:
            raise RuntimeError(f"获取微信 CDN 上传参数失败: {up_resp}")
        session = await self._ensure_session()
        async with session.post(
            cdn_url,
            data=encrypted,
            headers={"Content-Type": "application/octet-stream"},
            proxy=self.effective_proxy,
            timeout=aiohttp.ClientTimeout(total=30.0),
        ) as cdn_resp:
            if cdn_resp.status != 200:
                raise RuntimeError(f"上传图片至微信 CDN 失败: HTTP {cdn_resp.status}")
            download_param = str(cdn_resp.headers.get("x-encrypted-param") or "").strip()
        if not download_param:
            raise RuntimeError("微信 CDN 上传成功但未返回 x-encrypted-param")

        client_id = f"agy_img_{uuid.uuid4().hex[:14]}"
        aes_b64 = base64.b64encode(aes_hex.encode("ascii")).decode("ascii")
        send_payload = {
            "msg": {
                "from_user_id": "",
                "to_user_id": to_uid,
                "client_id": client_id,
                "message_type": 2,
                "message_state": 2,
                "context_token": ctx_tok,
                "item_list": [
                    {
                        "type": 2,
                        "image_item": {
                            "media": {
                                "encrypt_query_param": download_param,
                                "aes_key": aes_b64,
                                "encrypt_type": 1,
                            },
                            "mid_size": len(encrypted),
                        },
                    }
                ],
            },
            "base_info": {"channel_version": _CHANNEL_VERSION},
        }
        res = await self._post_json("/ilink/bot/sendmessage", send_payload, timeout_seconds=15.0)
        errcode = int(res.get("errcode") or res.get("ret") or 0)
        if errcode != 0:
            raise RuntimeError(f"微信发送图片消息失败 (errcode={errcode}): {res.get('errmsg') or res}")

        if (caption or "").strip():
            await self.send_text(target.scene, to_uid, caption.strip(), context_token=ctx_tok)
        return client_id

    async def resolve_image_source(self, attachment: MediaAttachment) -> dict[str, str]:
        """Download and AES-128-ECB decrypt inbound WeChat CDN images into local cache."""
        if not attachment.file_id:
            return {}
        try:
            meta = json.loads(attachment.file_id)
        except Exception:
            return {}
        enc_param = str(meta.get("encrypt_query_param") or "").strip()
        full_url = str(meta.get("full_url") or "").strip()
        if not enc_param and not full_url:
            return {}
        aes_key = parse_ilink_aes_key(str(meta.get("aes_key") or ""))
        dl_url = full_url or f"{self.cdn_base}/download?encrypted_query_param={urllib.parse.quote(enc_param)}"
        key_seed = enc_param or full_url
        try:
            session = await self._ensure_session()
            async with session.get(dl_url, proxy=self.effective_proxy, timeout=aiohttp.ClientTimeout(total=20.0)) as resp:
                if resp.status != 200:
                    return {}
                raw = await resp.read()
            if aes_key is not None:
                raw = aes128_ecb_decrypt(raw, aes_key)
            self._media_cache_dir.mkdir(parents=True, exist_ok=True)
            ext = ".png" if raw.startswith(b"\x89PNG") else (".gif" if raw.startswith(b"GIF8") else ".jpg")
            fname = f"wx_{hashlib.md5(key_seed.encode('utf-8')).hexdigest()[:14]}{ext}"
            out_path = self._media_cache_dir / fname
            out_path.write_bytes(raw)
            return {"local_path": str(out_path)}
        except Exception as exc:
            logger.warning("[%s] 下载/解密微信图片失败: %s", self.adapter_id, exc)
            return {}
