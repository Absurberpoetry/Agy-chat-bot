from __future__ import annotations

import base64
import datetime as dt
import re
import time
from typing import Any

import aiohttp

from agychat_bot.plugins import BotPlugin, PluginContext, PluginReply, command

_FLAG_VALUE_RE = re.compile(
    r"(?:^|\s)--(size|quality|format|complexity|background|frames|duration|model)\s+(\S+)",
    re.IGNORECASE,
)


def _parse_prompt_and_flags(raw_args: str) -> tuple[str, dict[str, str]]:
    """Extract optional `--key value` flags from a command string while preserving the rest as prompt."""
    flags: dict[str, str] = {}

    def _collect(m: re.Match[str]) -> str:
        flags[m.group(1).lower()] = m.group(2).strip()
        return " "

    cleaned = _FLAG_VALUE_RE.sub(_collect, raw_args or "")
    prompt = re.sub(r"\s+", " ", cleaned).strip()
    return prompt, flags


def _join_url(base: str, path: str) -> str:
    b = (base or "").strip().rstrip("/")
    p = (path or "").strip()
    if not p:
        p = "/v1/images/generations"
    if not p.startswith("/"):
        p = "/" + p
    return f"{b}{p}"


def _detect_ext(data: bytes, fallback: str = "png") -> str:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "gif"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return fallback.lower().lstrip(".") or "png"


class ImageGifGenPlugin(BotPlugin):
    id = "image_gif_gen"
    name = "AI 生图与动图 (GPT-Image-2.6)"
    version = "1.0.0"
    author = "AgyChat Bot"
    description = "基于 GPT-Image-2.6 协议生成静态图片与动态 GIF（支持生图/生 GIF 模型与上游独立配置）"

    default_config: dict[str, Any] = {
        "api_base": "https://yuzapi.fun",
        "api_key": "sk-38433286550efe1230c30b068bce995ef0f208020297591909a08307c03a6989",
        "images_path": "/v1/images/generations",
        "models_path": "/v1/models",
        # 静态生图模型与默认参数
        "image_model": "gpt-image-2.6-sunburst",
        "image_size": "1024x1024",
        "image_quality": "medium",
        "image_output_format": "png",
        "image_complexity": "",
        "image_background": "auto",
        # 动态 GIF 独立模型与默认参数（gif_api_base/gif_api_key/gif_path 留空时自动复用上方主配置）
        "gif_api_base": "",
        "gif_api_key": "",
        "gif_path": "",
        "gif_model": "gpt-image-2.6-sunburst",
        "gif_size": "1024x1024",
        "gif_quality": "medium",
        "gif_frame_count": 9,
        "gif_frame_duration_ms": 200,
        "gif_complexity": "high",
        # 运行与回复样式
        "timeout_seconds": 180,
        "use_bot_proxy": False,
        "send_wait_notice": True,
        "quote_reply": True,
        "at_user": False,
        "save_to_disk": True,
    }

    config_schema: list[dict[str, Any]] = [
        # Group 1: 主上游接口
        {
            "key": "api_base",
            "label": "上游接口地址",
            "description": "OpenAI Images 兼容服务根地址，末尾无需加斜杠",
            "type": "string",
            "group": "上游接口连接",
            "placeholder": "https://yuzapi.fun",
        },
        {
            "key": "api_key",
            "label": "API 密钥 (Key)",
            "description": "Bearer 鉴权密钥",
            "type": "password",
            "group": "上游接口连接",
            "placeholder": "sk-...",
        },
        {
            "key": "images_path",
            "label": "生图接口路径",
            "description": "默认为 /v1/images/generations",
            "type": "string",
            "group": "上游接口连接",
            "placeholder": "/v1/images/generations",
        },
        {
            "key": "models_path",
            "label": "模型列表路径",
            "description": "用于 /imgmodels 查询上游可用模型，默认为 /v1/models",
            "type": "string",
            "group": "上游接口连接",
            "placeholder": "/v1/models",
        },
        # Group 2: 静态生图 (/draw)
        {
            "key": "image_model",
            "label": "生图模型标识",
            "description": "调用 /draw（/画图 /生图）时使用的模型 ID",
            "type": "string",
            "group": "静态生图配置（/draw）",
            "placeholder": "gpt-image-2.6-sunburst",
        },
        {
            "key": "image_size",
            "label": "默认图片尺寸",
            "description": "生成静态图片的宽高比",
            "type": "select",
            "group": "静态生图配置（/draw）",
            "options": [
                {"value": "1024x1024", "label": "1024x1024（方形 1:1）"},
                {"value": "1536x1024", "label": "1536x1024（横版 3:2）"},
                {"value": "1024x1536", "label": "1024x1536（竖版 2:3）"},
                {"value": "auto", "label": "auto（模型自动决定）"},
            ],
        },
        {
            "key": "image_quality",
            "label": "生图画质档位",
            "description": "画质越高细节越丰富，耗时与成本也相应增加",
            "type": "select",
            "group": "静态生图配置（/draw）",
            "options": [
                {"value": "low", "label": "low（快速省流）"},
                {"value": "medium", "label": "medium（均衡推荐）"},
                {"value": "high", "label": "high（高精细度）"},
                {"value": "auto", "label": "auto（默认）"},
            ],
        },
        {
            "key": "image_output_format",
            "label": "静态图输出格式",
            "description": "PNG 无损最清晰，WebP/JPEG 体积更小",
            "type": "select",
            "group": "静态生图配置（/draw）",
            "options": [
                {"value": "png", "label": "PNG（无损）"},
                {"value": "jpeg", "label": "JPEG（体积小）"},
                {"value": "webp", "label": "WebP（高压缩）"},
            ],
        },
        {
            "key": "image_complexity",
            "label": "构图复杂度",
            "description": "GPT-Image-2.6 专属规划力度参数，留空则不传",
            "type": "select",
            "group": "静态生图配置（/draw）",
            "options": [
                {"value": "", "label": "不指定（跟随模型默认）"},
                {"value": "low", "label": "low（简洁）"},
                {"value": "medium", "label": "medium（适中）"},
                {"value": "high", "label": "high（精细规划）"},
            ],
        },
        {
            "key": "image_background",
            "label": "背景模式",
            "description": "透明背景需配合 PNG 或 WebP 格式使用",
            "type": "select",
            "group": "静态生图配置（/draw）",
            "options": [
                {"value": "auto", "label": "auto（自动）"},
                {"value": "opaque", "label": "opaque（不透明背景）"},
                {"value": "transparent", "label": "transparent（透明背景）"},
            ],
        },
        # Group 3: 动态 GIF (/gif)
        {
            "key": "gif_model",
            "label": "GIF 专用模型标识",
            "description": "调用 /gif（/动图 /生gif）时使用的模型 ID，与静态生图模型完全独立",
            "type": "string",
            "group": "动态 GIF 独立配置（/gif）",
            "placeholder": "gpt-image-2.6-sunburst",
        },
        {
            "key": "gif_size",
            "label": "GIF 画面尺寸",
            "description": "动态图的默认分辨率",
            "type": "select",
            "group": "动态 GIF 独立配置（/gif）",
            "options": [
                {"value": "512x512", "label": "512x512（轻量表情包）"},
                {"value": "1024x1024", "label": "1024x1024（高清方形）"},
                {"value": "1536x1024", "label": "1536x1024（横版）"},
                {"value": "1024x1536", "label": "1024x1536（竖版）"},
                {"value": "auto", "label": "auto（自动）"},
            ],
        },
        {
            "key": "gif_quality",
            "label": "GIF 画质档位",
            "description": "控制单帧渲染精细度",
            "type": "select",
            "group": "动态 GIF 独立配置（/gif）",
            "options": [
                {"value": "low", "label": "low（快速）"},
                {"value": "medium", "label": "medium（均衡推荐）"},
                {"value": "high", "label": "high（高精细）"},
                {"value": "auto", "label": "auto（默认）"},
            ],
        },
        {
            "key": "gif_frame_count",
            "label": "默认帧数 (frame_count)",
            "description": "GIF 动画总帧数，范围 2 ~ 16 帧",
            "type": "number",
            "group": "动态 GIF 独立配置（/gif）",
            "min": 2,
            "max": 16,
            "step": 1,
        },
        {
            "key": "gif_frame_duration_ms",
            "label": "每帧时长 (毫秒)",
            "description": "单帧停留时间（毫秒，自动按 10ms 对齐，如 200ms = 5fps）",
            "type": "number",
            "group": "动态 GIF 独立配置（/gif）",
            "min": 20,
            "max": 2000,
            "step": 10,
        },
        {
            "key": "gif_complexity",
            "label": "GIF 动作复杂度",
            "description": "越高动作衔接越连贯",
            "type": "select",
            "group": "动态 GIF 独立配置（/gif）",
            "options": [
                {"value": "", "label": "不指定（跟随默认）"},
                {"value": "low", "label": "low"},
                {"value": "medium", "label": "medium"},
                {"value": "high", "label": "high（推荐）"},
            ],
        },
        {
            "key": "gif_api_base",
            "label": "GIF 独立上游地址（选填）",
            "description": "留空则复用上方主上游地址；若 GIF 使用其他中转站可在此单独填写",
            "type": "string",
            "group": "动态 GIF 独立配置（/gif）",
            "placeholder": "留空复用主上游地址",
        },
        {
            "key": "gif_api_key",
            "label": "GIF 独立 API 密钥（选填）",
            "description": "留空则复用上方主 API 密钥",
            "type": "password",
            "group": "动态 GIF 独立配置（/gif）",
            "placeholder": "留空复用主密钥",
        },
        {
            "key": "gif_path",
            "label": "GIF 独立接口路径（选填）",
            "description": "留空则复用上方生图路径（/v1/images/generations）",
            "type": "string",
            "group": "动态 GIF 独立配置（/gif）",
            "placeholder": "留空复用主路径",
        },
        # Group 4: 交互与运行
        {
            "key": "timeout_seconds",
            "label": "请求超时时间（秒）",
            "description": "高清图或 GIF 生成耗时较长，建议保持 120~180 秒",
            "type": "number",
            "group": "交互与运行选项",
            "min": 15,
            "max": 600,
            "step": 5,
        },
        {
            "key": "use_bot_proxy",
            "label": "使用机器人全局代理",
            "description": "开启后请求上游生图接口时走系统配置的代理，直连正常请保持关闭",
            "type": "boolean",
            "group": "交互与运行选项",
        },
        {
            "key": "send_wait_notice",
            "label": "先发送「正在生成」提示",
            "description": "触发后先发一条排队提示，避免群友以为没收到指令",
            "type": "boolean",
            "group": "交互与运行选项",
        },
        {
            "key": "quote_reply",
            "label": "发送图片时引用原消息",
            "description": "将生成的图片或 GIF 以引用回复的方式发回群聊",
            "type": "boolean",
            "group": "交互与运行选项",
        },
        {
            "key": "at_user",
            "label": "发送图片时 @ 触发用户",
            "description": "群聊场景下同时 @ 发起画图指令的用户",
            "type": "boolean",
            "group": "交互与运行选项",
        },
        {
            "key": "save_to_disk",
            "label": "本地归档生成的图片与 GIF",
            "description": "自动保存一份至 data/plugins/image_gif_gen/outputs/",
            "type": "boolean",
            "group": "交互与运行选项",
        },
    ]

    async def on_load(self) -> None:
        self.ctx.logger.info(
            "AI 生图/GIF 插件已就绪 (api=%s, image_model=%s, gif_model=%s)",
            self.config.get("api_base", ""),
            self.config.get("image_model", ""),
            self.config.get("gif_model", ""),
        )

    def _resolve_proxy(self) -> str | None:
        if not bool(self.config.get("use_bot_proxy", False)):
            return None
        p = (self.ctx.bot_config.agy.proxy or "").strip()
        return p or None

    async def _request_generation(
        self,
        *,
        api_base: str,
        api_key: str,
        endpoint_path: str,
        payload: dict[str, Any],
    ) -> tuple[bytes, dict[str, Any]]:
        if not api_base.strip():
            raise ValueError("未配置上游 api_base")
        if not api_key.strip():
            raise ValueError("未配置上游 api_key")

        url = _join_url(api_base, endpoint_path)
        timeout_sec = max(15.0, float(self.config.get("timeout_seconds", 180) or 180))
        proxy = self._resolve_proxy()
        headers = {
            "Authorization": f"Bearer {api_key.strip()}",
            "Content-Type": "application/json",
            "User-Agent": "agychat-bot-image-gif-plugin/1.0",
        }
        timeout = aiohttp.ClientTimeout(total=timeout_sec)

        async with aiohttp.ClientSession(timeout=timeout, trust_env=False) as session:
            async with session.post(url, json=payload, headers=headers, proxy=proxy) as resp:
                raw_text = await resp.text(errors="replace")
                if resp.status >= 400:
                    err_msg = raw_text[:400]
                    try:
                        err_obj = await resp.json(content_type=None)
                        if isinstance(err_obj, dict):
                            err_inner = err_obj.get("error")
                            if isinstance(err_inner, dict) and err_inner.get("message"):
                                err_msg = str(err_inner["message"])
                            elif err_obj.get("message"):
                                err_msg = str(err_obj["message"])
                    except Exception:
                        pass
                    raise RuntimeError(f"HTTP {resp.status}: {err_msg}")

                try:
                    data_obj = await resp.json(content_type=None)
                except Exception as exc:
                    raise RuntimeError(f"上游返回非 JSON 数据: {raw_text[:200]}") from exc

            items = data_obj.get("data") if isinstance(data_obj, dict) else None
            if not isinstance(items, list) or not items or not isinstance(items[0], dict):
                raise RuntimeError(f"上游响应中缺少 data 图像结果: {str(data_obj)[:240]}")

            first = items[0]
            if first.get("b64_json"):
                media_bytes = base64.b64decode(str(first["b64_json"]))
                return media_bytes, data_obj

            img_url = str(first.get("url") or "").strip()
            if img_url:
                async with session.get(img_url, proxy=proxy) as img_resp:
                    if img_resp.status != 200:
                        raise RuntimeError(f"下载生成结果失败 HTTP {img_resp.status}")
                    media_bytes = await img_resp.read()
                    return media_bytes, data_obj

            raise RuntimeError("上游响应既无 b64_json 也无 url 字段")

    def _save_output_if_enabled(self, data: bytes, *, prefix: str, fmt_hint: str) -> str:
        if not bool(self.config.get("save_to_disk", True)):
            return ""
        out_dir = self.ctx.data_dir / "outputs"
        out_dir.mkdir(parents=True, exist_ok=True)
        ext = _detect_ext(data, fallback=fmt_hint)
        ts = dt.datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]
        out_path = out_dir / f"{prefix}_{ts}.{ext}"
        out_path.write_bytes(data)
        return str(out_path)

    async def generate_image(
        self,
        prompt: str,
        *,
        flags: dict[str, str] | None = None,
    ) -> tuple[bytes, dict[str, Any], float]:
        f = flags or {}
        model = f.get("model") or str(self.config.get("image_model") or "gpt-image-2.6-sunburst").strip()
        size = f.get("size") or str(self.config.get("image_size") or "1024x1024").strip()
        quality = f.get("quality") or str(self.config.get("image_quality") or "medium").strip()
        out_fmt = (f.get("format") or str(self.config.get("image_output_format") or "png")).strip().lower()
        if out_fmt not in ("png", "jpeg", "jpg", "webp"):
            out_fmt = "png"
        if out_fmt == "jpg":
            out_fmt = "jpeg"
        complexity = (f.get("complexity") or str(self.config.get("image_complexity") or "")).strip().lower()
        background = (f.get("background") or str(self.config.get("image_background") or "")).strip().lower()

        payload: dict[str, Any] = {
            "model": model,
            "prompt": prompt,
            "size": size,
            "quality": quality,
            "output_format": out_fmt,
        }
        if complexity in ("low", "medium", "high"):
            payload["complexity"] = complexity
        if background in ("auto", "transparent", "opaque"):
            payload["background"] = background

        t0 = time.monotonic()
        media_bytes, resp_obj = await self._request_generation(
            api_base=str(self.config.get("api_base") or ""),
            api_key=str(self.config.get("api_key") or ""),
            endpoint_path=str(self.config.get("images_path") or "/v1/images/generations"),
            payload=payload,
        )
        elapsed = round(time.monotonic() - t0, 2)
        self._save_output_if_enabled(media_bytes, prefix="img", fmt_hint=out_fmt)
        return media_bytes, resp_obj, elapsed

    async def generate_gif(
        self,
        prompt: str,
        *,
        flags: dict[str, str] | None = None,
    ) -> tuple[bytes, dict[str, Any], float]:
        f = flags or {}
        model = f.get("model") or str(self.config.get("gif_model") or "gpt-image-2.6-sunburst").strip()
        size = f.get("size") or str(self.config.get("gif_size") or "1024x1024").strip()
        quality = f.get("quality") or str(self.config.get("gif_quality") or "medium").strip()
        complexity = (f.get("complexity") or str(self.config.get("gif_complexity") or "")).strip().lower()

        try:
            frame_count = int(f.get("frames") or self.config.get("gif_frame_count") or 9)
        except (TypeError, ValueError):
            frame_count = 9
        frame_count = max(2, min(16, frame_count))

        try:
            frame_duration = int(f.get("duration") or self.config.get("gif_frame_duration_ms") or 200)
        except (TypeError, ValueError):
            frame_duration = 200
        frame_duration = max(20, int(round(frame_duration / 10.0) * 10))

        payload: dict[str, Any] = {
            "model": model,
            "prompt": prompt,
            "output_format": "gif",
            "frame_count": frame_count,
            "frame_duration_ms": frame_duration,
            "size": size,
            "quality": quality,
        }
        if complexity in ("low", "medium", "high"):
            payload["complexity"] = complexity

        gif_base = str(self.config.get("gif_api_base") or "").strip() or str(self.config.get("api_base") or "")
        gif_key = str(self.config.get("gif_api_key") or "").strip() or str(self.config.get("api_key") or "")
        gif_path = str(self.config.get("gif_path") or "").strip() or str(
            self.config.get("images_path") or "/v1/images/generations"
        )

        t0 = time.monotonic()
        media_bytes, resp_obj = await self._request_generation(
            api_base=gif_base,
            api_key=gif_key,
            endpoint_path=gif_path,
            payload=payload,
        )
        elapsed = round(time.monotonic() - t0, 2)
        self._save_output_if_enabled(media_bytes, prefix="gif", fmt_hint="gif")
        return media_bytes, resp_obj, elapsed

    @command("draw", aliases=["画图", "生图", "img", "image"], description="调用 GPT-Image-2.6 生成图片")
    async def cmd_draw(self, ctx: PluginContext, msg, args: str) -> PluginReply:
        prompt, flags = _parse_prompt_and_flags(args)
        quote = bool(self.config.get("quote_reply", True))
        at = bool(self.config.get("at_user", False))
        if not prompt:
            return PluginReply(
                text=(
                    "用法：/draw <画面描述>（可选参数：--size 1024x1024 --quality low|medium|high "
                    "--format png|jpeg|webp --complexity low|medium|high --model <模型>）"
                ),
                quote=quote,
                at=at,
            )

        model = flags.get("model") or str(self.config.get("image_model") or "gpt-image-2.6-sunburst")
        if bool(self.config.get("send_wait_notice", True)):
            await ctx.send_reply(msg, f"正在使用 {model} 生成图片，请稍候...", quote=quote, at=False)

        try:
            img_bytes, _, elapsed = await self.generate_image(prompt, flags=flags)
            await ctx.send_image(msg, img_bytes, quote=quote, at=at)
            ctx.logger.info("生图成功 (model=%s, bytes=%d, elapsed=%.2fs)", model, len(img_bytes), elapsed)
            return PluginReply(text="", stop=True)
        except Exception as exc:
            ctx.logger.warning("生图失败 (model=%s): %s", model, exc)
            return PluginReply(text=f"生图失败：{exc}", quote=quote, at=at)

    @command("gif", aliases=["动图", "生gif", "画gif"], description="调用 GPT-Image-2.6 生成动态 GIF")
    async def cmd_gif(self, ctx: PluginContext, msg, args: str) -> PluginReply:
        prompt, flags = _parse_prompt_and_flags(args)
        quote = bool(self.config.get("quote_reply", True))
        at = bool(self.config.get("at_user", False))
        if not prompt:
            return PluginReply(
                text=(
                    "用法：/gif <动态画面描述>（可选参数：--frames 2~16 --duration 200 "
                    "--size 1024x1024 --quality medium --complexity high --model <模型>）"
                ),
                quote=quote,
                at=at,
            )

        model = flags.get("model") or str(self.config.get("gif_model") or "gpt-image-2.6-sunburst")
        if bool(self.config.get("send_wait_notice", True)):
            await ctx.send_reply(msg, f"正在使用 {model} 生成动态 GIF，请稍候...", quote=quote, at=False)

        try:
            gif_bytes, _, elapsed = await self.generate_gif(prompt, flags=flags)
            await ctx.send_image(msg, gif_bytes, quote=quote, at=at)
            ctx.logger.info("生 GIF 成功 (model=%s, bytes=%d, elapsed=%.2fs)", model, len(gif_bytes), elapsed)
            return PluginReply(text="", stop=True)
        except Exception as exc:
            ctx.logger.warning("生 GIF 失败 (model=%s): %s", model, exc)
            extra_hint = ""
            if "gif output is not enabled" in str(exc).lower():
                extra_hint = "\n提示：当前上游账号/模型暂未开通 GIF 输出权限，可在 WebUI 插件配置中单独修改 gif_model / gif_api_base / gif_api_key。"
            return PluginReply(text=f"生成 GIF 失败：{exc}{extra_hint}", quote=quote, at=at)

    @command("imgmodels", aliases=["生图模型", "画图模型"], description="查看上游可用生图/GIF模型列表")
    async def cmd_models(self, ctx: PluginContext, msg, args: str) -> PluginReply:
        quote = bool(self.config.get("quote_reply", True))
        api_base = str(self.config.get("api_base") or "").strip()
        api_key = str(self.config.get("api_key") or "").strip()
        models_path = str(self.config.get("models_path") or "/v1/models").strip()
        url = _join_url(api_base, models_path)
        headers = {"Authorization": f"Bearer {api_key}", "User-Agent": "agychat-bot-image-gif-plugin/1.0"}
        try:
            timeout = aiohttp.ClientTimeout(total=15.0)
            async with aiohttp.ClientSession(timeout=timeout, trust_env=False) as session:
                async with session.get(url, headers=headers, proxy=self._resolve_proxy()) as resp:
                    if resp.status >= 400:
                        return PluginReply(text=f"拉取模型列表失败: HTTP {resp.status}", quote=quote)
                    obj = await resp.json(content_type=None)
            ids = [
                str(m.get("id"))
                for m in (obj.get("data") or [])
                if isinstance(m, dict) and m.get("id")
            ]
            img_ids = [i for i in ids if "image" in i.lower() or "gif" in i.lower() or "flux" in i.lower()]
            shown = img_ids or ids
            reply = (
                f"当前生图模型: {self.config.get('image_model')}\n"
                f"当前 GIF 模型: {self.config.get('gif_model')}\n"
                f"上游可用图像模型 ({len(shown)}): {', '.join(shown) if shown else '无'}"
            )
            return PluginReply(text=reply, quote=quote)
        except Exception as exc:
            return PluginReply(text=f"拉取模型列表失败: {exc}", quote=quote)
