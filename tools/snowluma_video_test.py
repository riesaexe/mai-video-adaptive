# -*- coding: utf-8 -*-
"""SnowLuma 视频来源、消息分段匹配和 NapCat 隔离回归测试。"""
import asyncio
import sys
import tempfile
import types
from pathlib import Path

# plugin.py 依赖宿主 SDK；测试用最小占位模块，不连接 MaiBot。
_sdk = types.ModuleType("maibot_sdk")


class _Base:
    pass


_sdk.Field = lambda default=None, **_kw: default
_sdk.HookHandler = lambda *a, **k: (lambda f: f)
_sdk.MaiBotPlugin = _Base
_sdk.PluginConfigBase = _Base
_sdk.Tool = lambda *a, **k: (lambda f: f)
sys.modules["maibot_sdk"] = _sdk

_t = types.ModuleType("maibot_sdk.types")
_t.ErrorPolicy = type("ErrorPolicy", (), {"SKIP": 1})
_t.HookMode = type("HookMode", (), {"BLOCKING": 1, "NON_BLOCKING": 2})
_t.HookOrder = type("HookOrder", (), {"NORMAL": 1})
_t.ToolParameterInfo = lambda **kw: kw
_t.ToolParamType = type("ToolParamType", (), {
    "STRING": "string", "FLOAT": "float",
    "INTEGER": "integer", "BOOLEAN": "boolean",
})
sys.modules["maibot_sdk.types"] = _t

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import plugin as P  # noqa: E402

ok = fail = 0


def check(name, condition, extra=""):
    global ok, fail
    if condition:
        ok += 1
        print(f"  [PASS] {name} {extra}")
    else:
        fail += 1
        print(f"  [FAIL] {name} {extra}")


class _Logger:
    def info(self, *args, **kwargs):
        pass

    def warning(self, *args, **kwargs):
        pass


print("[1] 入站 SnowLuma 客户端类型传入异步处理")


async def _route_message():
    routed = []
    inst = P.VideoUnderstandPlugin.__new__(P.VideoUnderstandPlugin)
    inst.config = types.SimpleNamespace(
        plugin=types.SimpleNamespace(enabled=True),
        source=types.SimpleNamespace(auto_process=True, max_videos_per_message=3),
    )
    inst.ctx = types.SimpleNamespace(logger=_Logger())
    inst._bg = set()

    async def capture(*args):
        routed.append(args)

    inst._handle = capture
    msg = {
        "processed_plain_text": "[视频] 文件: clip-1.mp4，大小: 1000",
        "message_info": {
            "group_info": {"group_id": "group-1"},
            "additional_config": {"client_type": "SnowLuma"},
        },
        "message_id": "message-1",
        "session_id": "session-1",
    }
    await inst.on_after_process(msg)
    tasks = list(inst._bg)
    if tasks:
        await asyncio.gather(*tasks)
    return routed


routed = asyncio.run(_route_message())
check("处理任务收到规范化的 snowluma 类型",
      len(routed) == 1 and routed[0][4] == "snowluma",
      f"-> {routed[0][4] if routed else None}")

print("\n[2] 原始消息视频 URL 必须与当前占位素材匹配")
response = {"data": {"message": [
    {"type": "video", "data": {
        "file": "clip-1.mp4",
        "url": "https://cdn.example/clip-1.mp4?signature=fixture",
    }},
    {"type": "video", "data": {
        "file": "clip-2.mp4",
        "url": "https://cdn.example/clip-2.mp4?signature=fixture",
    }},
]}}
asset = P.media_mod.VideoAsset(name="clip-1.mp4", file_ref="clip-1.mp4")
selected = P.VideoUnderstandPlugin._snowluma_video_url(response, asset)
check("多视频时精确匹配文件名", selected.endswith("/clip-1.mp4?signature=fixture"))

try:
    P.VideoUnderstandPlugin._snowluma_video_url(
        {"message": [response["data"]["message"][0]]},
        P.media_mod.VideoAsset(name="unrelated.mp4", file_ref="unrelated.mp4"),
    )
except RuntimeError:
    mismatch_rejected = True
else:
    mismatch_rejected = False
check("唯一 URL 也不得回退到不匹配的素材", mismatch_rejected)

url_only_asset = P.media_mod.VideoAsset(
    url="https://cdn.example/clip-1.mp4?signature=old-fixture")
check("URL-only 素材按原 URL 文件名匹配",
      P.VideoUnderstandPlugin._snowluma_video_url(response, url_only_asset) ==
      "https://cdn.example/clip-1.mp4?signature=fixture")

try:
    P.VideoUnderstandPlugin._snowluma_video_url(
        {"message": [{"type": "video", "data": {
            "url": "https://cdn.example/clip-2.mp4?signature=fixture",
        }}]},
        url_only_asset,
    )
except RuntimeError:
    single_url_mismatch_rejected = True
else:
    single_url_mismatch_rejected = False
check("单视频 URL-only 素材也拒绝不匹配的文件名",
      single_url_mismatch_rejected)

url_only_message = {"raw_message": [
    {"type": "video", "data": {
        "url": "https://cdn.example/clip-1.mp4?signature=fixture",
    }},
    {"type": "video", "data": {
        "file": "clip-2.mp4",
        "url": "https://cdn.example/clip-2.mp4?signature=fixture",
    }},
]}
url_only_asset = P.media_mod.extract_assets(url_only_message)[0]
partial_response = {"message": [
    {"type": "video", "data": {"file": "clip-1.mp4"}},
    response["data"]["message"][1],
]}
try:
    P.VideoUnderstandPlugin._snowluma_video_url(partial_response, url_only_asset)
except RuntimeError:
    partial_mismatch_rejected = True
else:
    partial_mismatch_rejected = False
check("多视频中 URL-only 素材不误取另一个唯一 URL",
      partial_mismatch_rejected)

single = {"message": [response["data"]["message"][0]]}
check("没有素材标识时保留唯一 URL 回退",
      P.VideoUnderstandPlugin._snowluma_video_url(
          single, P.media_mod.VideoAsset()) ==
      "https://cdn.example/clip-1.mp4?signature=fixture")

print("\n[3] SnowLuma 通过 get_msg 取回，并绕过 NapCat 暂存目录")
with tempfile.TemporaryDirectory() as tmp:
    calls = []
    materialized = []

    async def fake_api_call(api_name, **kwargs):
        calls.append((api_name, kwargs))
        return single

    async def unexpected_fetch(*_args, **_kwargs):
        raise AssertionError("SnowLuma 不应读取 NapCat 暂存目录")

    async def fake_materialize(asset, **kwargs):
        materialized.append((asset.url, kwargs))
        return Path(tmp) / "materialized.mp4"

    inst = P.VideoUnderstandPlugin.__new__(P.VideoUnderstandPlugin)
    inst.config = types.SimpleNamespace(
        source=types.SimpleNamespace(max_video_mb=5),
        audio=types.SimpleNamespace(timeout_s=30),
        napcat=types.SimpleNamespace(fetch_dir=str(Path(tmp) / "napcat-staging")),
    )
    inst.ctx = types.SimpleNamespace(
        logger=_Logger(),
        paths=types.SimpleNamespace(runtime_dir=tmp),
        api=types.SimpleNamespace(call=fake_api_call),
    )
    inst._wait_fetched = unexpected_fetch
    original_materialize = P.media_mod.materialize
    P.media_mod.materialize = fake_materialize
    try:
        result = asyncio.run(inst._materialize(
            asset, message_id="message-2", client_type="snowluma"))
    finally:
        P.media_mod.materialize = original_materialize

    check("调用 SnowLuma get_msg v1 并传入原消息 ID",
          calls == [("adapter.snowluma.message.get_msg",
                     {"version": "1", "message_id": "message-2"})])
    check("URL 交给受限的统一落盘函数",
          result.name == "materialized.mp4" and
          materialized[0][0] == "https://cdn.example/clip-1.mp4?signature=fixture")

print("\n[4] SnowLuma 清理不得删除 NapCat 同名暂存视频")
with tempfile.TemporaryDirectory() as tmp:
    staged = Path(tmp) / "shared-name.mp4"
    staged.write_bytes(b"napcat video")
    clean_asset = P.media_mod.VideoAsset(name=staged.name)
    inst = P.VideoUnderstandPlugin.__new__(P.VideoUnderstandPlugin)
    inst.config = types.SimpleNamespace(
        napcat=types.SimpleNamespace(fetch_dir=tmp),
        timeline=types.SimpleNamespace(keep_video=False),
    )
    inst.ctx = types.SimpleNamespace(
        logger=_Logger(), paths=types.SimpleNamespace(runtime_dir=str(Path(tmp) / "runtime")))
    inst._cleanup(clean_asset, None, "", "snowluma")
    check("SnowLuma 清理保留 NapCat 文件", staged.is_file())
    inst._cleanup(clean_asset, None, "", "napcat")
    check("NapCat 清理仍移除自身暂存文件", not staged.exists())

print(f"\n结果：{ok} 通过 / {fail} 失败")
sys.exit(1 if fail else 0)
