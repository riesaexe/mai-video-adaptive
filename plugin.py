# -*- coding: utf-8 -*-
"""视频理解插件（自适应抽帧 + 语音转录）。

检测入站视频，按内容自适应抽帧、可选转录语音，交给视觉模型理解，
把结果注入 bot 可见上下文（不直接对用户发言）。

核心（抽帧 / 音频 / 签名去重）在 adaptive_frames 里，本文件只做接线。
"""

from __future__ import annotations

import asyncio
import base64
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from maibot_sdk import Field, HookHandler, MaiBotPlugin, PluginConfigBase, Tool
from maibot_sdk.types import (ErrorPolicy, HookMode, HookOrder,
                              ToolParameterInfo, ToolParamType)

try:
    from . import adaptive_frames as af
    from . import media as media_mod
    from . import timeline_store as tl_store
except ImportError:  # PluginLoader 以文件方式加载
    import adaptive_frames as af  # type: ignore
    import media as media_mod  # type: ignore
    import timeline_store as tl_store  # type: ignore


_M_PENDING = "[视频解析]"
_M_DONE = "[视频内容]"
_M_FAIL = "[视频理解失败]"


# ------------------------------------------------------------------
# 配置
# ------------------------------------------------------------------

class PluginSection(PluginConfigBase):
    __ui_label__ = "插件"
    __ui_icon__ = "video"
    __ui_order__ = 0

    enabled: bool = Field(default=True, description="插件总开关",
                          json_schema_extra={"label": "启用插件", "order": 10})
    config_version: str = Field(default="1.0.0", description="配置版本（版本策略要求，一般无需改）",
                                json_schema_extra={"label": "配置版本", "order": 20})


class ExtractSection(PluginConfigBase):
    __ui_label__ = "抽帧"
    __ui_order__ = 1

    ffmpeg_path: str = Field(
        default="",
        description="ffmpeg 可执行文件绝对路径；留空则用 PATH（麦麦进程可能读不到新 PATH）",
        json_schema_extra={"label": "ffmpeg 路径", "order": 5})
    ffprobe_path: str = Field(
        default="",
        description="ffprobe 可执行文件绝对路径；留空则用 PATH",
        json_schema_extra={"label": "ffprobe 路径", "order": 6})
    seconds_per_frame: float = Field(
        default=4.0, description="有效内容的取样密度：每几秒一帧",
        json_schema_extra={"label": "每几秒一帧", "order": 10})
    min_frames: int = Field(
        default=0, description="帧数下限；0 表示由时长与新颖率自适应",
        json_schema_extra={"label": "最少帧数（0=自适应）", "order": 20})
    max_frames: int = Field(
        default=0, description="帧数上限；0 表示自适应（硬顶 48）",
        json_schema_extra={"label": "最多帧数（0=自适应）", "order": 30})
    max_height: int = Field(
        default=720, description="抽帧后的最大高度",
        json_schema_extra={"label": "最大高度", "order": 40})


class AudioSection(PluginConfigBase):
    __ui_label__ = "音频"
    __ui_order__ = 2

    mode: str = Field(
        default="off", description="off=不读音频 / local=本地模型 / api=云端接口",
        json_schema_extra={"label": "音频模式（off/local/api）", "order": 10})
    api_key: str = Field(default="", description="云端 ASR 的 API Key（api 模式用）",
                         json_schema_extra={"label": "ASR API Key", "order": 20})
    api_url: str = Field(default=af.SF_ASR_URL, description="云端 ASR 接口地址",
                         json_schema_extra={"label": "ASR 接口", "order": 30})
    api_model: str = Field(default=af.DEFAULT_ASR_MODEL, description="云端 ASR 模型名",
                           json_schema_extra={"label": "ASR 模型", "order": 40})
    local_model: str = Field(default="", description="本地模型路径（model.int8.onnx）",
                             json_schema_extra={"label": "本地模型路径", "order": 50})
    local_tokens: str = Field(default="", description="本地词表路径（tokens.txt）",
                              json_schema_extra={"label": "本地词表路径", "order": 60})
    timeout_s: float = Field(default=30.0, description="云端转录超时，超时即降级只交帧",
                             json_schema_extra={"label": "转录超时（秒）", "order": 70})
    min_free_mb: float = Field(
        default=500.0,
        description="加载本地模型前要求的最小可用内存（MB）；不达标就不加载",
        json_schema_extra={"label": "本地模式内存门槛（MB）", "order": 80})
    fallback: bool = Field(default=True, description="首选途径失败时回退另一种（API↔本地）",
                           json_schema_extra={"label": "失败回退", "order": 90})
    unload_after: bool = Field(default=True, description="转录后卸载本地模型，释放内存",
                               json_schema_extra={"label": "用完卸载", "order": 100})
    block_seconds: float = Field(
        default=120.0, description="单块音频最长秒数；越大调用越少、越快",
        json_schema_extra={"label": "单块最长（秒）", "order": 110})
    gap_seconds: float = Field(
        default=6.0,
        description="语音之间停顿短于该值就并成一块；越大调用越少，但会多送静音",
        json_schema_extra={"label": "停顿合并（秒）", "order": 120})
    max_audio_seconds: float = Field(
        default=600.0, description="只转录前 N 秒音频，0 表示不限；防超长视频",
        json_schema_extra={"label": "总时长上限（秒，0=不限）", "order": 130})


class VisionSection(PluginConfigBase):
    __ui_label__ = "视觉模型"
    __ui_order__ = 3

    mode: str = Field(
        default="host", description="host=走主程序任务 / direct=直连接口",
        json_schema_extra={"label": "调用方式（host/direct）", "order": 10})
    host_task: str = Field(
        default="vlm", description="host 模式走的任务名",
        json_schema_extra={"label": "主程序任务名", "order": 20})
    api_url: str = Field(default=af.DS_CHAT_URL, description="direct 模式的接口地址",
                         json_schema_extra={"label": "接口地址", "order": 30})
    api_key: str = Field(default="", description="direct 模式的 API Key",
                         json_schema_extra={"label": "API Key", "order": 40})
    model: str = Field(default=af.DEFAULT_VISION_MODEL, description="direct 模式的模型名",
                       json_schema_extra={"label": "模型名", "order": 50})
    timeout_s: float = Field(default=120.0, description="direct 模式超时",
                             json_schema_extra={"label": "超时（秒）", "order": 60})


class CacheSection(PluginConfigBase):
    __ui_label__ = "缓存"
    __ui_order__ = 4

    enabled: bool = Field(default=True, description="相似视频复用描述",
                          json_schema_extra={"label": "启用签名缓存", "order": 10})
    max_entries: int = Field(default=200, description="最多缓存条数",
                             json_schema_extra={"label": "最大条数", "order": 20})
    match_threshold: float = Field(default=0.9, description="签名命中阈值（越高越严）",
                                   json_schema_extra={"label": "命中阈值", "order": 30})


class NapcatSection(PluginConfigBase):
    __ui_label__ = "NapCat"
    __ui_order__ = 5

    enabled: bool = Field(default=True, description="用 NapCat HTTP get_file 取回真实视频",
                          json_schema_extra={"label": "启用 NapCat 取回", "order": 10})
    http_base_url: str = Field(default="http://127.0.0.1:3000",
                               description=("NapCat OneBot HTTP 地址（默认 3000）；"
                                            "仅在未配 fetch_dir、走 get_file 回退时用"),
                               json_schema_extra={"label": "HTTP Base URL", "order": 20})
    access_token: str = Field(default="", description="可选 access token",
                              json_schema_extra={"label": "Access Token", "order": 30})
    fetch_dir: str = Field(
        default="",
        description="NapCat 取回插件（video-fetch）的下载目录；填了则优先从这里取视频",
        json_schema_extra={"label": "取回目录", "order": 40})
    fetch_wait_s: float = Field(default=60.0, description="等待取回目录出现文件的最长秒数",
                                json_schema_extra={"label": "等待秒数", "order": 50})
    fetch_keep_hours: float = Field(
        default=24.0,
        description=("取回暂存目录里文件的最长保留小时数，超过即删；"
                     "暂存区只是中转站，要留的原片由 keep_video 另存（0=不限）"),
        json_schema_extra={"label": "暂存清理（小时）", "order": 60})


class SourceSection(PluginConfigBase):
    __ui_label__ = "视频源"
    __ui_order__ = 6

    auto_process: bool = Field(default=True, description="自动处理入站视频",
                               json_schema_extra={"label": "自动处理", "order": 10})
    cleanup_after: bool = Field(
        default=True,
        description="理解完成后删除视频与中间文件（帧/音频）；描述已入缓存，不影响复用",
        json_schema_extra={"label": "完成后删除文件", "order": 15})
    max_video_mb: float = Field(default=80.0, description="超过该大小跳过",
                                json_schema_extra={"label": "最大体积（MB）", "order": 20})
    max_videos_per_message: int = Field(default=3, description="单条消息最多处理几个视频",
                                        json_schema_extra={"label": "单条上限", "order": 30})
    concurrency: int = Field(default=1, description="同时处理数",
                             json_schema_extra={"label": "并发数", "order": 40})


class TimelineSection(PluginConfigBase):
    __ui_label__ = "时间轴"
    __ui_order__ = 7

    enabled: bool = Field(
        default=True,
        description="保留带时间戳的分段描述，支持「某一时刻是什么」这类提问",
        json_schema_extra={"label": "启用时间轴", "order": 10})
    ttl_days: float = Field(default=7.0, description="时间轴保留天数",
                            json_schema_extra={"label": "保留天数", "order": 20})
    max_entries: int = Field(default=200, description="最多保留条数",
                             json_schema_extra={"label": "最大条数", "order": 30})
    keep_video: bool = Field(
        default=False,
        description="保留原片，解锁「重读某段」工具；占磁盘，按下面小时数自动清理",
        json_schema_extra={"label": "保留原片（占磁盘）", "order": 40})
    keep_video_hours: float = Field(default=24.0, description="原片保留小时数",
                                    json_schema_extra={"label": "原片保留（小时）", "order": 50})
    inject_fresh_seconds: float = Field(
        default=300.0,
        description=("视频刚理解完的这段时间内，每条请求都注入摘要；"
                     "超过后仅在最近消息提到视频时才注入（省 token）"),
        json_schema_extra={"label": "强制注入时长（秒）", "order": 60})


class VideoUnderstandConfig(PluginConfigBase):
    __ui_label__ = "视频理解"

    plugin: PluginSection = Field(default_factory=PluginSection)
    extract: ExtractSection = Field(default_factory=ExtractSection)
    audio: AudioSection = Field(default_factory=AudioSection)
    vision: VisionSection = Field(default_factory=VisionSection)
    cache: CacheSection = Field(default_factory=CacheSection)
    napcat: NapcatSection = Field(default_factory=NapcatSection)
    source: SourceSection = Field(default_factory=SourceSection)
    timeline: TimelineSection = Field(default_factory=TimelineSection)


# ------------------------------------------------------------------
# 插件
# ------------------------------------------------------------------

class VideoUnderstandPlugin(MaiBotPlugin):
    """视频理解插件。"""

    config_model = VideoUnderstandConfig

    def __init__(self) -> None:
        super().__init__()
        self._sem: asyncio.Semaphore | None = None
        self._session_latest: dict[str, dict[str, Any]] = {}
        self._bg: set[asyncio.Task[Any]] = set()
        self._store: Any = None
        self._last_fetch_purge: float = 0.0

    # ---- 生命周期 ----

    async def on_load(self) -> None:
        self._sem = asyncio.Semaphore(max(1, int(self.config.source.concurrency)))
        Path(self.ctx.paths.runtime_dir).mkdir(parents=True, exist_ok=True)
        if self.config.timeline.enabled:
            self._store = tl_store.TimelineStore(
                self.ctx.paths.data_dir,
                ttl_days=float(self.config.timeline.ttl_days),
                max_entries=int(self.config.timeline.max_entries))
        self.ctx.logger.info(
            "时间轴初始化 enabled=%s store=%s data_dir=%s",
            self.config.timeline.enabled,
            self._store is not None,
            self.ctx.paths.data_dir)
        # 指定 ffmpeg / ffprobe（进程继承的 PATH 可能不含它们）
        ff = str(self.config.extract.ffmpeg_path or "").strip()
        fp = str(self.config.extract.ffprobe_path or "").strip()
        af.set_tools(ff or None, fp or None)
        if self.config.timeline.keep_video:
            try:
                n = self._purge_kept()
                if n:
                    self.ctx.logger.info("清理过期原片 %d 个", n)
            except Exception:  # noqa: BLE001
                pass
        # 取回暂存区按小时清理：它只是中转站，不设闸就会只涨不落
        try:
            n = self._purge_fetch_dir()
            if n:
                self.ctx.logger.info("清理取回暂存区 %d 个过期文件", n)
        except Exception:  # noqa: BLE001
            pass
        await self._sync_read_video_state()
        import shutil as _sh
        self.ctx.logger.info(
            "视频理解插件已加载 extract=%s/s audio=%s cache=%s ffmpeg=%s",
            self.config.extract.seconds_per_frame,
            self.config.audio.mode,
            self.config.cache.enabled,
            af.FFMPEG if not ff else ff)

    async def on_unload(self) -> None:
        for task in list(self._bg):
            task.cancel()
        self._bg.clear()

    async def on_config_update(self, scope: str, config_data: dict[str, Any],
                               version: str) -> None:
        del scope, config_data, version
        self._sem = asyncio.Semaphore(max(1, int(self.config.source.concurrency)))
        await self._sync_read_video_state()

    async def _sync_read_video_state(self) -> None:
        """按当前配置热切换 read_video 的启用态。

        宿主侧只翻内存态，重启后以 get_components 的声明为准，两者互补。
        """
        comp = getattr(self.ctx, "component", None)
        if comp is None:
            return
        fn = (comp.enable_component if bool(self.config.timeline.keep_video)
              else comp.disable_component)
        try:
            await fn("read_video", "tool")
        except Exception as exc:  # noqa: BLE001
            self.ctx.logger.warning("切换 read_video 启用态失败：%s", exc)

    # ---- Hook：检测入站视频 ----

    @HookHandler(
        "chat.receive.after_process",
        name="video_understand_after_process",
        description="检测视频并异步理解，结果注入上下文",
        mode=HookMode.BLOCKING,
        order=HookOrder.NORMAL,
        error_policy=ErrorPolicy.SKIP,
    )
    async def on_after_process(self, message: Any = None, **kwargs: Any) -> dict[str, Any] | None:
        del kwargs
        if not self.config.plugin.enabled or not self.config.source.auto_process:
            return None
        if not isinstance(message, dict):
            return None

        assets = media_mod.extract_assets(message)
        if not assets:
            return None

        limit = max(1, int(self.config.source.max_videos_per_message))
        assets = assets[:limit]
        stream_id = self._stream_id(message)

        plain = str(message.get("processed_plain_text") or "").strip()
        # 这句会写进消息文本并永久保留（收到时写的，事后改不了）。
        # 因此不能写「正在理解」之类带时态的话——否则 bot 的历史里
        # 永远挂着一条「处理中」，被问到时就说「还没好 / 加载不出来」。
        # 用中性描述：只说内容由插件提供，不说进度。
        line = f"{_M_PENDING} 本条视频的内容由解析插件提供"
        if _M_PENDING not in plain and _M_DONE not in plain:
            message["processed_plain_text"] = f"{plain}\n{line}".strip() if plain else line

        msg_info = message.get("message_info")
        group_info = (msg_info.get("group_info")
                      if isinstance(msg_info, dict) else None)
        group_id = (str(group_info.get("group_id") or "")
                    if isinstance(group_info, dict) else "")
        additional_config = (msg_info.get("additional_config")
                             if isinstance(msg_info, dict) else None)
        client_type = (str(additional_config.get("client_type") or "").strip().lower()
                       if isinstance(additional_config, dict) else "")
        message_id = str(message.get("message_id") or "")
        self.ctx.logger.info("检测到 %d 个视频 session=%s group=%s msg=%s",
                             len(assets), stream_id or "-", group_id or "-",
                             message_id or "-")
        for asset in assets:
            task = asyncio.create_task(
                self._handle(asset, stream_id, group_id, message_id, client_type))
            self._bg.add(task)
            task.add_done_callback(self._bg.discard)

        return {"action": "continue", "modified_kwargs": {"message": message}}

    # ---- Hook：模型请求前注入 ----

    @HookHandler(
        "maisaka.replyer.before_model_request",
        name="video_understand_inject",
        description="在模型请求前注入最近一次视频理解结果",
        mode=HookMode.BLOCKING,
        order=HookOrder.NORMAL,
        error_policy=ErrorPolicy.SKIP,
    )
    async def on_before_model_request(self, messages: Any = None,
                                      **kwargs: Any) -> dict[str, Any] | None:
        """模型请求前注入最近一次视频理解结果。

        注意：这个钩子给的是麦麦自己的 ContextItem 列表（kwargs['items']），
        不是 OpenAI 风格的 messages。
        """
        if not self.config.plugin.enabled:
            return None

        items = kwargs.get("items")
        if not isinstance(items, list):
            return None

        sid = str(kwargs.get("session_id") or kwargs.get("stream_id") or "").strip()
        record = self._session_latest.get(sid) if sid else None
        # 严格按会话匹配，绝不跨会话兜底：
        # 一旦拿本会话记录去填别的会话（群 A 的视频注进群 B/私聊），就是串台。
        if not record:
            return None
        # 陈旧记录不再注入（30 分钟），避免会话 id 被复用时带出旧内容
        if time.time() - float(record.get("ts") or 0.0) > 1800:
            self._session_latest.pop(sid, None)
            return None
        if not str(record.get("text") or "").strip():
            return None

        # 相关性闸门：
        # 实测注入的 item 是「单次请求有效」的——麦麦每次请求都重建 items，
        # 所以上下文里看不到上次注入的内容，那个 _M_DONE 检查永不命中，
        # 结果 10 分钟内把同一份摘要重注了 7 次（纯白付 token）。
        # 因此：刚上传的短窗口内直接注；过窗口后只在最近消息提到视频时才注。
        fresh_s = float(getattr(self.config.timeline, "inject_fresh_seconds", 300) or 0)
        age = time.time() - float(record.get("ts") or 0.0)
        if fresh_s > 0 and age > fresh_s and not self._mentions_video(items):
            return None

        text = str(record["text"]).strip()
        ref = str(record.get("key") or "").strip()
        # 自报家门：明确告诉模型「这段文字就是你刚看过的视频内容」，
        # 否则它会当成第三方给的描述，回答「我看不到视频 / 只看到一串描述」。
        if ref:
            header = (f"{_M_DONE} 你已经看过这条视频了（编号 #{ref}），"
                      "以下是它的画面与语音内容，可直接据此回答：")
        else:
            header = (f"{_M_DONE} 你已经看过这条视频了，"
                      "以下是它的画面与语音内容，可直接据此回答：")

        # 已在上下文里则不重复注入
        for it in items:
            if not isinstance(it, dict):
                continue
            for part in (it.get("parts") or []):
                if isinstance(part, dict) and _M_DONE in str(part.get("text") or ""):
                    return None

        import uuid as _uuid
        from datetime import datetime as _dt

        block = f"{header}\n{text}"
        item = {
            "item_type": "SystemMessageItem",
            "meta": {
                "item_id": _uuid.uuid4().hex,
                "logical_turn_id": None,
                "timestamp": _dt.now().isoformat(),
            },
            "parts": [{"type": "text", "text": block}],
        }
        new_items = list(items)
        # 追加到末尾：保持前缀（人设 + 历史）不变，命中 prompt 缓存
        new_items.append(item)
        # 整包回传：只替换 items，其余参数（含 item_schema_version）原样带回去
        new_kwargs = dict(kwargs)
        new_kwargs["items"] = new_items
        if not new_kwargs.get("item_schema_version"):
            new_kwargs["item_schema_version"] = 1
        self.ctx.logger.info("已注入视频内容（末尾，schema=%s）：%s",
                             new_kwargs.get("item_schema_version"), block[:120])
        return {"action": "continue", "modified_kwargs": new_kwargs}

    # 提到视频的较弱信号，宁漏勿错（漏了只是不注，错了会白付 token）
    _VIDEO_HINTS = ("视频", "录像", "录屏", "这段", "那一段")

    @classmethod
    def _mentions_video(cls, items: Any, lookback: int = 6) -> bool:
        """最近几条上下文里是否提到视频。"""
        if not isinstance(items, list):
            return False
        for it in reversed(items[-lookback:]):
            if not isinstance(it, dict):
                continue
            for part in (it.get("parts") or []):
                if not isinstance(part, dict):
                    continue
                t = str(part.get("text") or "")
                if not t or _M_DONE in t:
                    continue
                if any(h in t for h in cls._VIDEO_HINTS):
                    return True
        return False

    def _trim_session_latest(self, keep: int = 50) -> None:
        """会话记录条数上限，超出按时间淘汰，避免长期运行内存增长。"""
        if len(self._session_latest) <= keep:
            return
        order = sorted(self._session_latest.items(),
                       key=lambda kv: float(kv[1].get("ts") or 0.0))
        for sid, _ in order[:len(self._session_latest) - keep]:
            self._session_latest.pop(sid, None)

    def _remember(self, desc: dict[str, Any], prep: dict[str, Any],
                  stream_id: str, group_id: str = "",
                  message_id: str = "", file_name: str = "") -> str:
        """完整时间轴落盘，返回注入上下文用的短标识。

        摘要常驻上下文，完整时间轴落盘按需查（见 README 的设计原则）。
        已存在则保留首次记录，不覆盖，避免后到的缓存命中把 message_id 清掉。
        """
        store = self._store
        sig = prep.get("sig")
        if store is None or not sig:
            return ""
        try:
            key = tl_store.sig_key(sig)
            if store.load(key):
                return key
            store.save(key, segments=desc.get("segments") or [],
                       summary=desc.get("summary") or desc.get("text") or "",
                       duration=float(prep.get("duration") or 0.0),
                       group_id=group_id or stream_id,
                       message_id=message_id,
                       file_name=file_name,
                       video_kept=bool(self.config.timeline.keep_video))
            self.ctx.logger.info(
                "时间轴已落盘 key=%s group=%s msg=%s segs=%d",
                key, group_id or stream_id, message_id or "-",
                len(desc.get("segments") or []))
            return key
        except Exception as exc:  # noqa: BLE001
            self.ctx.logger.warning("时间轴落盘失败：%s", exc)
            return ""

    def _reuse_by_filename(self, asset: media_mod.VideoAsset, stream_id: str,
                           message_id: str = "", why: str = "原片已清理") -> bool:
        """按文件名找回已有时间轴直接复用。

        两个入口：
          - 取回前先查（省掉 60 秒的取回等待）
          - 取回失败后兜底
        文件名是 QQ 按内容生成的，同一条视频重发名字不变。
        """
        store = self._store
        if store is None:
            return False
        name = str(asset.name or asset.file_ref or "").strip()
        key = store.by_filename(name)
        if not key:
            return False
        summary = store.summary(key)
        if not summary:
            return False
        rec = store.load(key) or {}
        store.set_message_id(key, message_id)
        self._session_latest[stream_id] = {
            "text": summary, "ts": time.time(), "key": key,
            "segments": rec.get("segments") or [], "summary": summary}
        self._trim_session_latest()
        self.ctx.logger.info("%s，按文件名复用已有时间轴 key=%s name=%s",
                             why, key, name[:60])
        return True

    # ---- ③ 级：保留原片后的按需重读 ----

    def _purge_fetch_dir(self) -> int:
        """清理取回暂存区里的过期文件，返回删除数。

        暂存区只是中转站：要长期留的原片由 keep_video 复制到 kept_videos/，
        所以这里的东西过期即可删。

        不加这道闸的实测后果：7 天堆到 46 个文件 / 667MB，
        而整段日志里清理只触发了 18 次（检测到视频 158 次）。
        漏的原因有好几个（重启抓死任务、合并转发里下了但认不出、文件名对不上），
        靠堵单个漏点不可靠，按时间清才对所有漏点都成立。
        """
        base = str(self.config.napcat.fetch_dir or "").strip()
        if not base:
            return 0
        hours = float(self.config.napcat.fetch_keep_hours or 0)
        if hours <= 0:
            return 0
        d = Path(base)
        if not d.is_dir():
            return 0
        cutoff = time.time() - hours * 3600
        n = 0
        for p in d.glob("*"):
            # latest.json 是取回插件的台账，不能删
            if not p.is_file() or p.suffix.lower() == ".json":
                continue
            try:
                if p.stat().st_mtime < cutoff:
                    p.unlink()
                    n += 1
            except Exception:  # noqa: BLE001
                pass
        return n

    def _maybe_purge_fetch(self, min_interval_s: float = 1800.0) -> None:
        """运行中定期清暂存区（默认半小时最多扫一次，避免每条视频都扫目录）。"""
        now = time.time()
        if now - self._last_fetch_purge < min_interval_s:
            return
        self._last_fetch_purge = now
        try:
            n = self._purge_fetch_dir()
            if n:
                self.ctx.logger.info("清理取回暂存区 %d 个过期文件", n)
        except Exception as exc:  # noqa: BLE001
            self.ctx.logger.warning("清理取回暂存区失败：%s", exc)

    def _kept_dir(self) -> Path:
        return Path(self.ctx.paths.data_dir) / "kept_videos"

    def _kept_video_path(self, key: str) -> Path | None:
        d = self._kept_dir()
        if not d.is_dir():
            return None
        for p in d.glob(f"{key}.*"):
            if p.is_file():
                return p
        return None

    def _purge_kept(self) -> int:
        """按保留小时数清理过期原片。"""
        hours = float(self.config.timeline.keep_video_hours or 0)
        if hours <= 0:
            return 0
        cutoff = time.time() - hours * 3600
        n = 0
        for p in self._kept_dir().glob("*"):
            try:
                if p.is_file() and p.stat().st_mtime < cutoff:
                    p.unlink()
                    n += 1
            except Exception:  # noqa: BLE001
                pass
        return n

    def _extract_range(self, video: Path, start: float, end: float,
                       n: int = 8) -> list:
        """在 [start, end] 内固定密度补抽帧（区间小，要精度不要效率）。"""
        out = (Path(self.ctx.paths.runtime_dir) / "reread"
               / f"{video.stem}_{int(start)}_{int(end)}")
        step = max(0.5, (float(end) - float(start)) / max(1, n))
        times = [float(start) + i * step for i in range(n)]
        return af.extract_at(video, times, out,
                             max_height=int(self.config.extract.max_height))

    def _resolve_kept_video(self, ident: str) -> tuple[Path | None, str]:
        """把模型给的标识解析成原片路径。

        模型眼前有两个标识：消息文本里的文件名（NapCat 适配器写的）和注入里的
        「编号 #xxx」。它会挑哪个说不准，所以两边都得认：
          时间轴 key / 文件名 / 文件名去扩展名 / 带 # 的编号
        """
        raw = str(ident or "").strip().lstrip("#").strip()
        if not raw:
            return None, ""
        stem = Path(raw).stem
        # 1) 直接当时间轴 key（保留文件名就是 key）
        for cand in (raw, stem):
            p = self._kept_video_path(cand)
            if p is not None:
                return p, cand
        # 2) 当文件名反查时间轴 key
        store = self._store
        if store is not None:
            for cand in (raw, stem, f"{stem}.mp4"):
                k = store.by_filename(cand)
                if k:
                    p = self._kept_video_path(k)
                    if p is not None:
                        return p, k
        # 3) 兜底：在保留目录里按文件名对
        d = self._kept_dir()
        if d.is_dir():
            for f in d.glob("*"):
                if f.is_file() and (f.name == raw or f.stem == stem):
                    return f, f.stem
        return None, raw

    async def _read_segment(self, key: str, start: float, end: float) -> str:
        if not bool(self.config.timeline.keep_video):
            return "原片未保留，无法重读片段（可在配置里打开「保留原片」）。"
        video, key = self._resolve_kept_video(key)
        if video is None:
            return (f"没找到视频 {key or '?'} 的原片，可能已过期。"
                    "（可以用上下文里的 视频#编号，或视频文件名）")
        s = max(0.0, float(start))
        e = float(end)
        if e <= s:
            e = s + 10.0
        e = min(e, s + 120.0)
        frames = await asyncio.to_thread(self._extract_range, video, s, e)
        if not frames:
            return f"视频 #{key} 在 {s:.0f}~{e:.0f} 秒抽帧失败。"
        prompt = (f"这是一段视频 {af.fmt_ts(s)}~{af.fmt_ts(e)} 之间的画面。"
                  "请完整描述这段区间里发生了什么：画面中的物体、文字、动作与变化。"
                  "只写能从画面确认的内容，不推测。")
        cfg = self.config
        if str(cfg.vision.mode or "host").strip().lower() == "direct":
            res = await asyncio.to_thread(
                af.describe_video, frames, "", api_key=str(cfg.vision.api_key),
                url=str(cfg.vision.api_url), model=str(cfg.vision.model),
                timeout=float(cfg.vision.timeout_s))
            return str(res.get("text") or res.get("raw") or "").strip()
        content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
        for f in frames:
            b64 = base64.b64encode(Path(f).read_bytes()).decode("ascii")
            content.append({"type": "image", "image_format": "jpeg",
                            "image_base64": b64})
        result = await self.ctx.llm.generate(
            [{"role": "user", "content": content}],
            model=str(cfg.vision.host_task or "vlm"))
        if isinstance(result, dict):
            if result.get("success") is False:
                raise RuntimeError(str(result.get("error") or "宿主模型调用失败"))
            result = (result.get("response") or result.get("content")
                      or result.get("text") or "")
        return str(result or "").strip()

    def get_components(self) -> list[dict[str, Any]]:
        """声明组件启用态。

        未开「保留原片」时，把 read_video 置为禁用态：它不暴露给 planner，
        模型看不到也就不会白调。

        不直接剔除的原因（与社区插件一致）：组件不进注册表时，运行期再
        启用会因「未找到组件」失败；保留注册、只改启用态才可热切换。
        """
        components = super().get_components()
        try:
            keep = bool(self.config.timeline.keep_video)
        except Exception:  # noqa: BLE001  配置尚未注入时按默认（关）处理
            keep = False
        if keep:
            return components
        for comp in components:
            if comp.get("name") == "read_video":
                meta = comp.get("metadata")
                if isinstance(meta, dict):
                    meta["enabled"] = False
        return components

    @Tool(
        "read_video",
        description=("重看某条视频的某个时间段并描述细节。"
                     "video_id 用上下文里「视频#xxxxxxxx」中的那串标识。"),
        parameters=[
            ToolParameterInfo(name="video_id", param_type=ToolParamType.STRING,
                              description=("视频标识：上下文里的 视频#编号，"
                                           "或视频文件名，两者都可"),
                              required=True),
            ToolParameterInfo(name="start", param_type=ToolParamType.FLOAT,
                              description="起始秒数", required=True),
            ToolParameterInfo(name="end", param_type=ToolParamType.FLOAT,
                              description="结束秒数", required=True),
        ],
    )
    async def handle_read_video(self, video_id: str = "", start: float = 0,
                                end: float = 0, **kwargs: Any) -> dict[str, Any]:
        """按时间区间重读视频片段（需要已保留原片）。"""
        del kwargs
        try:
            text = await self._read_segment(str(video_id or "").strip(),
                                            float(start), float(end))
        except Exception as exc:  # noqa: BLE001
            self.ctx.logger.warning("重读片段失败 %s: %s", video_id, exc)
            text = f"重读片段失败：{exc}"
        self.ctx.logger.info("重读片段 video=%s %s~%s -> %s",
                             video_id, start, end, str(text)[:80])
        return {"name": "read_video", "content": text}

    # ---- 处理流水线 ----

    async def _handle(self, asset: media_mod.VideoAsset, stream_id: str,
                      group_id: str = "", message_id: str = "",
                      client_type: str = "") -> None:
        assert self._sem is not None
        video_path: Path | None = None
        name = str(asset.name or asset.file_ref or "")
        # 转发消息可能带发送方手机的路径（/storage/emulated/...），本地不存在，直接放弃
        if name.startswith("/"):
            self.ctx.logger.info("跳过转发消息内的视频（无本地实体）：%s", name[:80])
            return
        key = ""
        async with self._sem:
            try:
                # 先按文件名查已有结果：命中就不必等取回（那是 60 秒的等待），
                # 同一条视频被重发/引用时走的正是这条路。
                if self._reuse_by_filename(asset, stream_id, message_id,
                                           why="已有同名时间轴"):
                    return
                try:
                    video_path = await self._materialize(
                        asset, message_id=message_id, client_type=client_type)
                except FileNotFoundError:
                    # 兜底：上面的按名查没命中，但取回也失败
                    if self._reuse_by_filename(asset, stream_id, message_id):
                        return
                    raise
                prep = await asyncio.to_thread(self._prepare, video_path)
                if prep.get("cached"):
                    desc = {"cached": True,
                            "segments": prep.get("segments") or [],
                            "summary": prep.get("summary") or prep["text"],
                            "raw": prep["text"], "text": prep["text"]}
                else:
                    desc = await self._describe(
                        prep["frames"], prep["transcript"],
                        prep.get("frame_times"), prep.get("duration"))
                    # 声画双轨：把音频轨道并进摘要。
                    # 漏了这一步的话，语音内容只能经由 prompt 影响模型这一次输出，
                    # 进不了常驻上下文，之后任何一轮都看不到语气信息。
                    merged = af.merge_tracks(desc.get("segments") or [],
                                             prep.get("audio_segments") or [])
                    if merged:
                        desc["summary"] = merged
                        desc["text"] = merged
                    if prep.get("cache") is not None and desc.get("text"):
                        prep["cache"].remember(
                            prep["sig"], desc["text"],
                            asset.name or asset.file_ref,
                            segments=desc.get("segments"),
                            summary=desc.get("summary"))
                text = desc["text"]
                key = self._remember(desc, prep, stream_id, group_id, message_id,
                                     file_name=name)
                self._session_latest[stream_id] = {
                    "text": text, "ts": time.time(), "key": key,
                    "segments": desc.get("segments") or [],
                    "summary": desc.get("summary") or text}
                self._trim_session_latest()
                self.ctx.logger.info("视频理解完成 name=%s text=%s",
                                     asset.name or asset.file_ref, text[:80])
            except Exception as exc:  # noqa: BLE001
                import traceback
                self.ctx.logger.warning("视频理解失败 name=%s err=%s\n%s",
                                        asset.name or asset.file_ref, exc,
                                        traceback.format_exc()[-800:])
            finally:
                if bool(self.config.source.cleanup_after):
                    await asyncio.to_thread(self._cleanup, asset, video_path, key)
                await asyncio.to_thread(self._maybe_purge_fetch)

    @staticmethod
    def _safe_child(base: Path, name: str) -> Path | None:
        """把文件名拼到 base 下；越界（`..`、绝对路径、盘符）返回 None。

        asset.name / file_ref 是从消息文本里抽出来的，可以被伪造（例如骗造
        `[视频] 文件: ..\\..\\xxx`）。必须确保解析后仍落在 base 内，
        否则宁可不解引用。
        """
        raw = str(name or "").strip()
        if not raw:
            return None
        leaf = Path(raw.replace("\\", "/")).name
        if not leaf or leaf in (".", ".."):
            return None
        try:
            base_r = base.resolve()
            cand = (base / leaf).resolve()
        except Exception:  # noqa: BLE001
            return None
        if cand != base_r and base_r not in cand.parents:
            return None
        return cand

    def _cleanup(self, asset: media_mod.VideoAsset, video_path: Path | None,
                 key: str = "") -> None:
        """清理视频本体与中间产物（帧 / 音频）。描述已入签名缓存，删除不影响复用。

        keep_video 打开时，先把原片备份到数据目录，再照常清理。
        """
        import shutil as _sh
        removed = []

        # 0) 需要保留原片时先备份（供 read_video 工具重读片段）
        if key and bool(self.config.timeline.keep_video) and video_path is not None:
            src = Path(video_path)
            if src.is_file():
                try:
                    dst = self._kept_dir() / f"{key}{src.suffix or '.mp4'}"
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    _sh.copy2(src, dst)
                    removed.append(f"kept:{dst.name}")
                except Exception as exc:  # noqa: BLE001
                    self.ctx.logger.warning("保留原片失败 %s: %s", key, exc)

        # 1) 取回的视频本体（可能在 NapCat 下载目录，也可能在本地运行时目录）
        cands = []
        if video_path is not None:
            cands.append(Path(video_path))
        fetch_dir = str(self.config.napcat.fetch_dir or "").strip()
        if fetch_dir:
            base = Path(fetch_dir)
            for name in (asset.name, asset.file_ref):
                cand = self._safe_child(base, name)
                if cand is None:
                    if name:
                        self.ctx.logger.warning(
                            "拒绝可疑路径（越出取回目录）: %s", str(name)[:120])
                    continue
                cands.append(cand)
        for p in cands:
            try:
                if p.is_file():
                    p.unlink()
                    removed.append(str(p))
            except Exception as exc:  # noqa: BLE001
                self.ctx.logger.warning("删除视频失败 %s: %s", p, exc)

        # 2) 中间产物目录（frame/audio/孪生副本）
        base = Path(self.ctx.paths.runtime_dir) / "videos"
        targets = []
        if video_path is not None:
            targets.append(base / Path(video_path).stem)
        targets.append(base / asset.key[:16])
        for target in targets:
            try:
                if target.is_dir():
                    _sh.rmtree(target, ignore_errors=True)
                    removed.append(str(target))
            except Exception:  # noqa: BLE001
                pass

        if removed:
            self.ctx.logger.info("已清理视频文件 %d 项", len(removed))

    async def _materialize(self, asset: media_mod.VideoAsset, *,
                           message_id: str = "", client_type: str = "") -> Path:
        """落盘；SnowLuma 从原消息取回 URL，NapCat 保留现有取回流程。"""

        cfg = self.config
        is_snowluma = str(client_type or "").strip().lower() == "snowluma"
        runtime = Path(self.ctx.paths.runtime_dir) / "videos" / asset.key[:16]
        max_bytes = int(float(cfg.source.max_video_mb) * 1024 * 1024)
        timeout_s = max(5.0, float(cfg.audio.timeout_s))

        # 1) NapCat 取回插件的下载目录（video-fetch 插件把真实视频存这里）
        fetch_dir = str(cfg.napcat.fetch_dir or "").strip()
        if fetch_dir and not is_snowluma:
            got = await self._wait_fetched(fetch_dir, asset)
            if got is not None and got.stat().st_size <= max_bytes:
                return got
            # 配了取回目录却拿不到：不再回退 get_file（QQ 不下原片，那条路必死）
            self.ctx.logger.info("取回目录未拿到视频，放弃 name=%s", asset.name or asset.file_ref)
            raise FileNotFoundError(f"取回目录未找到视频：{asset.name or asset.file_ref}")

        # 2) 自带来源（url / base64 / 本地路径）
        direct_error: Exception | None = None
        if asset.url or asset.base64_data or asset.local_path:
            try:
                return await media_mod.materialize(
                    asset, target_dir=runtime, timeout_s=timeout_s, max_bytes=max_bytes)
            except Exception as exc:  # noqa: BLE001
                direct_error = exc
                if not is_snowluma and not (asset.file_ref or asset.name):
                    raise
                self.ctx.logger.info("直接落盘失败，尝试取回来源视频：%s", exc)

        # SnowLuma 的入站转换会把视频段变成文本占位，且不提供视频 get_file；
        # get_msg 返回的原始消息仍含有视频 URL。
        if is_snowluma:
            if not message_id:
                if direct_error is not None:
                    raise direct_error
                raise RuntimeError("SnowLuma 视频取回需要原消息 ID")
            response = await self.ctx.api.call(
                "adapter.snowluma.message.get_msg",
                version="1",
                message_id=message_id,
            )
            url = self._snowluma_video_url(response, asset)
            recovered = media_mod.VideoAsset(
                name=asset.name or Path(urlparse(url).path).name,
                file_ref=asset.file_ref,
                url=url,
                source=asset.source,
            )
            return await media_mod.materialize(
                recovered, target_dir=runtime, timeout_s=timeout_s, max_bytes=max_bytes)

        # 3) NapCat OneBot get_file
        ref = str(asset.file_ref or asset.name or "").strip()
        if not ref:
            raise ValueError("素材缺少 url / base64 / local_path / file_ref")
        raw = await self._fetch_via_napcat(ref)
        if len(raw) > max_bytes:
            raise ValueError(f"NapCat 取回视频过大：{len(raw)} > {max_bytes}")
        name = asset.name or ref
        return await asyncio.to_thread(
            media_mod.save_bytes, raw, target_dir=runtime, name=name, key=asset.key)

    @staticmethod
    def _snowluma_video_url(response: Any, asset: media_mod.VideoAsset) -> str:
        """从 SnowLuma get_msg 的原始消息中定位当前素材的视频 URL。"""

        detail = response
        if isinstance(detail, dict) and isinstance(detail.get("data"), dict):
            detail = detail["data"]
        if not isinstance(detail, dict):
            raise RuntimeError("SnowLuma get_msg 未返回消息详情")
        segments = detail.get("message")
        if not isinstance(segments, list):
            raise RuntimeError("SnowLuma get_msg 返回的消息没有原始分段")

        def name_key(value: Any) -> str:
            raw = str(value or "").strip().replace("\\", "/")
            if raw.lower().startswith(("http://", "https://")):
                raw = urlparse(raw).path
            return raw.rsplit("/", 1)[-1].casefold()

        asset_names = {name_key(value) for value in (asset.name, asset.file_ref) if value}
        asset_names.discard("")
        video_urls: set[str] = set()
        matching_urls: set[str] = set()
        for segment in segments:
            if not isinstance(segment, dict):
                continue
            if str(segment.get("type") or "").strip().lower() not in {
                "video", "short_video", "video_file",
            }:
                continue
            data = segment.get("data")
            if isinstance(data, str):
                url = data.strip()
                segment_names: set[str] = set()
            elif isinstance(data, dict):
                url = str(data.get("url") or data.get("file_url") or "").strip()
                segment_names = {
                    name_key(data.get(key))
                    for key in ("file", "file_id", "file_name", "filename", "name")
                    if data.get(key)
                }
            else:
                continue
            if not url.lower().startswith(("http://", "https://")):
                continue
            url_name = name_key(url)
            if url_name:
                segment_names.add(url_name)
            video_urls.add(url)
            if asset_names.intersection(segment_names):
                matching_urls.add(url)

        if len(matching_urls) == 1:
            return next(iter(matching_urls))
        if len(matching_urls) > 1:
            raise RuntimeError("SnowLuma 消息中有多个视频匹配当前素材，无法安全选择")
        if len(video_urls) == 1:
            return next(iter(video_urls))
        if not video_urls:
            raise RuntimeError("SnowLuma 原消息中没有可用的视频 URL")
        raise RuntimeError("SnowLuma 消息包含多个视频，无法与当前素材对应")

    async def _wait_fetched(self, fetch_dir: str, asset: media_mod.VideoAsset) -> Path | None:
        """等待取回目录里出现**当前这条且已写完**的视频。

    两个坑：
      1. latest.json 只保留最后一条记录，必须校验文件名，否则会拿错文件；
      2. 文件刚创建时就在写，必须等大小稳定，否则 ffprobe 拿到的是半截文件。
    """

        import json as _json

        names = [n for n in (asset.name, asset.file_ref) if n]
        if not names:
            return None
        name_set = {Path(n).name for n in names}
        deadline = time.time() + max(1.0, float(self.config.napcat.fetch_wait_s))
        dir_path = Path(fetch_dir)
        stable: dict[str, tuple[int, int]] = {}

        while time.time() < deadline:
            for name in name_set:
                cand = dir_path / name
                try:
                    size = cand.stat().st_size if cand.is_file() else -1
                except OSError:
                    size = -1
                if size <= 0:
                    stable.pop(name, None)
                    continue
                # 大小连续两次采样一致，才认为写完
                if stable.get(name, (-1, 0))[0] == size:
                    return cand
                stable[name] = (size, 0)
            await asyncio.sleep(1.5)
        return None

    async def _fetch_via_napcat(self, file_ref: str) -> bytes:
        cfg = self.config
        if not cfg.napcat.enabled:
            raise RuntimeError("NapCat 取回未启用")

        params = {"file_id": file_ref, "file": file_ref}

        # 优先走 adapter 能力
        try:
            result = await self.ctx.api.call("adapter.napcat.file.get_file", params=params)
            raw = self._bytes_from_napcat(response=result)
            if raw:
                return raw
        except Exception as exc:  # noqa: BLE001
            self.ctx.logger.info("adapter get_file 失败，回退 HTTP：%s", exc)

        # 回退裸 HTTP
        base = str(cfg.napcat.http_base_url or "").strip().rstrip("/")
        if not base:
            raise RuntimeError("NapCat http_base_url 为空")
        import json as _json
        import urllib.request as _ur

        body = _json.dumps(params).encode("utf-8")
        headers = {"Content-Type": "application/json",
                   "User-Agent": "maivideo-plugin/0.1"}
        token = str(cfg.napcat.access_token or "").strip()
        if token:
            headers["Authorization"] = f"Bearer {token}"

        def _post() -> dict[str, Any]:
            req = _ur.Request(f"{base}/get_file", data=body, headers=headers)
            with _ur.urlopen(req, timeout=max(10.0, float(cfg.source.max_video_mb))) as r:
                return _json.loads(r.read().decode("utf-8"))

        payload = await asyncio.to_thread(_post)
        raw = self._bytes_from_napcat(response=payload)
        if not raw:
            raise RuntimeError("NapCat get_file 未返回可用数据")
        return raw

    @staticmethod
    def _bytes_from_napcat(*, response: Any) -> bytes:
        data = response
        if isinstance(data, dict) and isinstance(data.get("data"), dict):
            data = data["data"]
        if not isinstance(data, dict):
            return b""

        b64 = str(data.get("base64") or data.get("base64_data") or "").strip()
        if b64:
            return base64.b64decode(b64.split(",")[-1], validate=False)

        # NapCat 常把本地文件路径放在 file / path / url 里（url 未必是网络地址）
        for key in ("file", "path", "file_path", "url"):
            cand = str(data.get(key) or "").strip()
            if not cand or cand.lower().startswith(("http://", "https://")):
                continue
            p = Path(cand)
            if p.is_file():
                return p.read_bytes()
        return b""

    # ---- 同步部分（在线程里跑） ----

    def _prepare(self, video: Path) -> dict[str, Any]:
        cfg = self.config
        runtime = Path(self.ctx.paths.runtime_dir) / "videos" / video.stem
        import shutil as _sh
        self.ctx.logger.info(
            "工具检查 ffmpeg=%s ffprobe=%s FFMPEG=%s",
            _sh.which("ffmpeg"), _sh.which("ffprobe"), af.FFMPEG)

        cache = None
        sig = None
        if cfg.cache.enabled:
            store = Path(self.ctx.paths.data_dir) / "video_sig_cache.json"
            cache = af.VideoSignatureCache(
                store, match_threshold=float(cfg.cache.match_threshold),
                max_entries=int(cfg.cache.max_entries))
            info = af.probe_media(video)
            sig = cache.signature(video, info["duration"])
            hit = cache.lookup(sig)
            if hit:
                return {"cached": True, "text": hit["text"], "sig": sig,
                        "duration": float(info.get("duration") or 0.0),
                        "segments": hit.get("segments") or [],
                        "summary": hit.get("summary") or hit["text"]}

        min_f = int(cfg.extract.min_frames) or None
        max_f = int(cfg.extract.max_frames) or None
        plan, frames = af.adaptive_extract(
            video, runtime / "frames", min_frames=min_f, max_frames=max_f,
            seconds_per_frame=float(cfg.extract.seconds_per_frame),
            max_height=int(cfg.extract.max_height))

        mode = str(cfg.audio.mode or "off").strip().lower()
        audio = af.audio_transcript(
            video, out_dir=runtime / "audio", mode=mode,
            api_key=str(cfg.audio.api_key or ""),
            local_model=cfg.audio.local_model or None,
            local_tokens=cfg.audio.local_tokens or None,
            model=str(cfg.audio.api_model or af.DEFAULT_ASR_MODEL),
            timeout=float(cfg.audio.timeout_s),
            min_free_mb=float(cfg.audio.min_free_mb),
            fallback=bool(cfg.audio.fallback),
            unload_after=bool(cfg.audio.unload_after),
            block_s=float(cfg.audio.block_seconds),
            gap_s=float(cfg.audio.gap_seconds),
            max_audio_s=float(cfg.audio.max_audio_seconds))
        transcript = "" if audio.get("skipped") else str(audio.get("text") or "")
        audio_segments = audio.get("segments") or []
        # 音频链路以前完全没日志：配了 api 却一直失败也不会有人知道。
        # 把结果打出来，才看得出到底是「没音频」还是「转了但失败」。
        self.ctx.logger.info(
            "音频 mode=%s used=%s skipped=%s reason=%s segs=%d chars=%d",
            mode, audio.get("used") or "-", audio.get("skipped"),
            audio.get("reason") or "-", len(audio_segments), len(transcript))

        return {"cached": False, "frames": frames, "transcript": transcript,
                "audio_segments": audio_segments,
                "sig": sig, "cache": cache,
                "frame_times": list(getattr(plan, "times", []) or []),
                "duration": float(getattr(plan, "duration", 0.0) or 0.0)}

    # ---- 异步：调用宿主模型 ----

    async def _describe(self, frames: list[Path], transcript: str,
                        frame_times=None, duration: float = None) -> dict[str, Any]:
        cfg = self.config
        mode = str(cfg.vision.mode or "host").strip().lower()

        if mode == "direct":
            if not str(cfg.vision.api_key or "").strip():
                raise RuntimeError("direct 模式未配置 API Key")
            return await asyncio.to_thread(
                af.describe_video, frames, transcript,
                frame_times=frame_times, duration=duration,
                api_key=str(cfg.vision.api_key),
                url=str(cfg.vision.api_url),
                model=str(cfg.vision.model),
                timeout=float(cfg.vision.timeout_s))

        if frame_times:
            prompt = af.build_timeline_prompt(frame_times, transcript)
        else:
            prompt = af.build_vision_prompt(transcript)
        content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
        for i, frame in enumerate(frames):
            if frame_times and i < len(frame_times):
                content.append({"type": "text",
                                "text": f"[{af.fmt_ts(frame_times[i])}]"})
            b64 = base64.b64encode(Path(frame).read_bytes()).decode("ascii")
            content.append({"type": "image", "image_format": "jpeg",
                            "image_base64": b64})
        result = await self.ctx.llm.generate(
            [{"role": "user", "content": content}],
            model=str(cfg.vision.host_task or "vlm"))

        if isinstance(result, dict):
            if result.get("success") is False:
                raise RuntimeError(str(result.get("error") or "宿主模型调用失败"))
            text = result.get("response") or result.get("content") or result.get("text") or ""
        else:
            text = result or ""
        text = str(text).strip()
        if not text:
            raise RuntimeError("宿主模型返回空描述")
        if frame_times:
            return af.parse_timeline(text, duration=duration)
        return {"segments": [], "summary": text, "raw": text, "text": text}

    # ---- 工具 ----

    @staticmethod
    def _stream_id(message: dict[str, Any]) -> str:
        for key in ("session_id", "stream_id", "chat_id"):
            value = message.get(key)
            if value:
                return str(value).strip()
        info = message.get("message_info") or {}
        if isinstance(info, dict):
            for key in ("session_id", "stream_id", "chat_id"):
                value = info.get(key)
                if value:
                    return str(value).strip()
        return ""


def create_plugin() -> VideoUnderstandPlugin:
    """插件工厂入口。"""

    return VideoUnderstandPlugin()
