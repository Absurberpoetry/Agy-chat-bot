from __future__ import annotations

import logging
import math
import random
import re

from .config import ContentSafetyConfig, ReplyConfig, SegmentedReplyConfig

logger = logging.getLogger(__name__)


_URL_OR_MD_IMG_RE = re.compile(r"!\[[^\]]*\]\([^)\r\n]+\)|https?://[^\s<>'\"`)]+", re.IGNORECASE)


def truncate_reply(text: str, max_chars: int) -> str:
    cleaned = text.strip()
    if max_chars <= 0 or len(cleaned) <= max_chars:
        return cleaned
    limit = max(1, max_chars - 3)
    # Avoid slicing through the middle of a Markdown image or URL
    for m in _URL_OR_MD_IMG_RE.finditer(cleaned):
        if m.start() < limit < m.end():
            if m.end() <= max_chars + 512:
                # Keep the intact image/URL rather than outputting a broken link
                return cleaned[: m.end()].rstrip()
            limit = max(1, m.start())
            break
    return cleaned[:limit].rstrip() + "..."


def _cleanup_segment(text: str, cleanup_rule: str) -> str:
    out = text
    if cleanup_rule:
        try:
            out = re.sub(cleanup_rule, "", out)
        except re.error as exc:
            logger.warning("分段回复清理正则无效 (%s): %s", cleanup_rule, exc)
    return out.strip()


_PUNCTUATION_SEPARATORS = {"。", "！", "？", "!", "?", "~", "～", "…", "；", ";"}


def _protect_urls(text: str) -> tuple[str, dict[str, str]]:
    tokens: dict[str, str] = {}

    def _repl(m: re.Match[str]) -> str:
        key = f"\x00URL{len(tokens)}\x00"
        tokens[key] = m.group(0)
        return key

    return _URL_OR_MD_IMG_RE.sub(_repl, text), tokens


def _restore_urls(text: str, tokens: dict[str, str]) -> str:
    if not tokens:
        return text
    out = text
    for k, v in tokens.items():
        out = out.replace(k, v)
    return out


def _split_by_words(text: str, split_words: list[str]) -> list[str]:
    """Split text whenever any custom word/delimiter in split_words appears.

    - Custom delimiters (e.g. '|||', '---', '<br>') are stripped as boundaries.
    - Sentence-ending punctuation (e.g. '。', '！', '？') stays attached to the preceding bubble.
    - Markdown images and HTTP(S) URLs are protected so query punctuation (? / ~ / !) never splits URLs.
    """
    tokens = [w for w in split_words if w]
    if not tokens:
        return [text]
    protected_text, url_map = _protect_urls(text)
    # Sort longer tokens first so '|||' matches before '|'
    tokens.sort(key=len, reverse=True)
    pattern = "(" + "|".join(re.escape(w) for w in tokens) + ")"
    pieces = re.split(pattern, protected_text)

    out: list[str] = []
    buf = ""
    for piece in pieces:
        if not piece:
            continue
        if piece in tokens:
            if piece in _PUNCTUATION_SEPARATORS:
                buf += piece
                if buf.strip():
                    out.append(_restore_urls(buf, url_map))
                buf = ""
            else:
                if buf.strip():
                    out.append(_restore_urls(buf, url_map))
                buf = ""
        else:
            buf += piece
    if buf.strip():
        out.append(_restore_urls(buf, url_map))
    return out


def _split_long_segment_safe(text: str, max_chars: int) -> list[str]:
    """Split a single overly long bubble into <= max_chars chunks without slicing Markdown images/URLs."""
    cleaned = text.strip()
    if max_chars <= 0 or len(cleaned) <= max_chars:
        return [cleaned] if cleaned else []

    out: list[str] = []
    pos = 0
    n = len(cleaned)
    while pos < n:
        rem = cleaned[pos:].strip()
        if not rem:
            break
        if len(rem) <= max_chars:
            out.append(rem)
            break
        # Find safe cut point <= max_chars
        cut = max_chars
        # Do not slice through a Markdown image or URL
        for m in _URL_OR_MD_IMG_RE.finditer(rem):
            if m.start() < cut < m.end():
                if m.start() > 0:
                    cut = m.start()
                else:
                    cut = m.end()
                break
        else:
            # Prefer natural newline / sentence punctuation boundary in the latter half
            min_boundary = max(1, max_chars // 2)
            for sep in ("\n", "。", "！", "？", "；", ".", "!", "?", "，", ",", " "):
                idx = rem.rfind(sep, min_boundary, cut)
                if idx != -1:
                    cut = idx + (0 if sep == "\n" else len(sep))
                    break
        chunk = rem[:cut].strip()
        if chunk:
            out.append(chunk)
        # Advance pos in original cleaned string
        leading_ws = len(cleaned[pos:]) - len(cleaned[pos:].lstrip())
        pos += leading_ws + max(1, cut)
    return out


def split_and_clean_reply(text: str, reply_cfg: ReplyConfig) -> list[str]:
    """Format and optionally split a model reply into multi-bubble segments (AstrBot compatible),
    enforcing total reply char cap, per-message char cap, and max message count when configured."""
    raw = text.strip()
    if not raw:
        return []

    total_cap = max(0, int(reply_cfg.max_reply_chars or 0))
    per_msg_cap = max(0, int(getattr(reply_cfg, "max_chars_per_message", 0) or 0))
    max_msgs = max(0, int(getattr(reply_cfg, "max_messages_per_reply", 0) or 0))

    if total_cap > 0:
        raw = truncate_reply(raw, total_cap)

    seg_cfg = reply_cfg.segmented_reply
    if not seg_cfg.enabled or len(raw) > seg_cfg.words_count_threshold:
        single = _cleanup_segment(raw, seg_cfg.content_cleanup_rule) if seg_cfg.enabled else raw
        if not single:
            return []
        cleaned = _split_long_segment_safe(single, per_msg_cap) if per_msg_cap > 0 else [single]
    else:
        parts: list[str] = []
        if seg_cfg.split_mode == "words" and seg_cfg.split_words:
            parts = _split_by_words(raw, seg_cfg.split_words)
        else:
            try:
                pattern = seg_cfg.regex or r"[\s\S]+?(?:\|\|\||$)"
                parts = [m.group(0) for m in re.finditer(pattern, raw) if m.group(0)]
            except re.error as exc:
                logger.warning("分段回复分割正则无效 (%s): %s", seg_cfg.regex, exc)
                parts = [raw]

        cleaned = []
        for part in parts:
            item = _cleanup_segment(part, seg_cfg.content_cleanup_rule)
            if not item:
                continue
            if per_msg_cap > 0 and len(item) > per_msg_cap:
                cleaned.extend(_split_long_segment_safe(item, per_msg_cap))
            else:
                cleaned.append(item)

        if not cleaned:
            fallback = _cleanup_segment(raw, seg_cfg.content_cleanup_rule)
            if fallback:
                cleaned = _split_long_segment_safe(fallback, per_msg_cap) if per_msg_cap > 0 else [fallback]
            else:
                return []

    if max_msgs > 0 and len(cleaned) > max_msgs:
        head = cleaned[: max_msgs - 1]
        tail_merged = "\n".join(cleaned[max_msgs - 1 :]).strip()
        if per_msg_cap > 0 and len(tail_merged) > per_msg_cap:
            tail_merged = truncate_reply(tail_merged, per_msg_cap)
        cleaned = [*head, tail_merged] if tail_merged else head

    if reply_cfg.reply_prefix and cleaned:
        cleaned[0] = f"{reply_cfg.reply_prefix}{cleaned[0]}"

    return cleaned


def compute_segment_delay(
    seg_cfg: SegmentedReplyConfig,
    *,
    text: str = "",
    rng: random.Random | None = None,
) -> float:
    """Compute delay in seconds between consecutive reply bubbles (supports random / fixed / log)."""
    if seg_cfg.interval_method == "log":
        base = max(1.1, float(seg_cfg.log_base or 2.6))
        chars = max(2, len(text.strip()))
        return min(8.0, max(0.3, math.log(chars, base)))

    spec = (seg_cfg.interval or "").strip()
    if not spec:
        return 1.5

    r = rng or random.Random()
    if seg_cfg.interval_method == "random" and "," in spec:
        pieces = [p.strip() for p in spec.split(",", 1)]
        try:
            lo = max(0.0, float(pieces[0]))
            hi = max(lo, float(pieces[1]))
            return r.uniform(lo, hi)
        except ValueError:
            return 1.5

    try:
        return max(0.0, float(spec.split(",")[0].strip()))
    except ValueError:
        return 1.5


def apply_content_safety(text: str, safety_cfg: ContentSafetyConfig) -> tuple[bool, str, str]:
    """Check text against content safety keywords. Returns (allowed, cleaned_text, matched_word)."""
    if not safety_cfg.enabled or not text or not safety_cfg.blocked_keywords:
        return True, text, ""

    low = text.lower()
    matched = [kw for kw in safety_cfg.blocked_keywords if kw and kw.lower() in low]
    if not matched:
        return True, text, ""

    if safety_cfg.action == "mask":
        out = text
        for kw in sorted(matched, key=len, reverse=True):
            out = re.sub(re.escape(kw), "***", out, flags=re.IGNORECASE)
        return True, out, matched[0]

    return False, text, matched[0]
