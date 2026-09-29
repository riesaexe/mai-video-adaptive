# -*- coding: utf-8 -*-
"""视频素材识别与落盘。

只做两件事：从消息里找出视频、把视频弄成本地文件。
不涉及理解逻辑（那部分在 adaptive_frames 里）。
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import http.client
import ipaddress
import re
import socket
import ssl
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

VIDEO_EXTENSIONS = {
    ".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v",
    ".flv", ".mpeg", ".mpg", ".3gp", ".ts",
}

# NapCat 适配器会把视频压成文本占位，例如：
#   [视频] 文件: xxx.mp4，大小: 2897329
#   [文件] Video_123.mp4，大小: 5694751，链接: https://...
_RE_VIDEO = re.compile(r"\[视频\]\s*(?:文件[:：]\s*)?(?P<name>[^，,；;\n]+)", re.IGNORECASE)
_RE_FILE = re.compile(r"\[文件\]\s*(?P<name>[^，,；;\n]+)", re.IGNORECASE)
_RE_LINK = re.compile(r"(?:链接|url)[:：]\s*(?P<url>https?://\S+)", re.IGNORECASE)


@dataclass
class VideoAsset:
    """一个待处理的视频素材。"""

    name: str = ""
    file_ref: str = ""
    url: str = ""
    local_path: str = ""
    base64_data: str = ""
    source: str = ""

    @property
    def key(self) -> str:
        material = "|".join([self.name, self.file_ref, self.url, self.local_path])
        return hashlib.sha256(material.encode("utf-8", "ignore")).hexdigest()


def _is_video_name(name: str, url: str = "") -> bool:
    for candidate in (name, urlparse(url or "").path):
        suffix = Path(str(candidate or "")).suffix.lower()
        if suffix in VIDEO_EXTENSIONS:
            return True
    return False


def _clean_name(raw: str) -> str:
    name = str(raw or "").strip()
    # 去掉尾部的大小/链接描述
    name = re.split(r"[，,；;]", name)[0].strip()
    return name


def extract_assets(message: dict[str, Any]) -> list[VideoAsset]:
    """从序列化消息里提取视频素材（结构化段 + NapCat 文本占位）。"""

    assets: list[VideoAsset] = []
    seen: set[str] = set()

    def add(asset: VideoAsset | None) -> None:
        if asset is None:
            return
        if asset.key in seen:
            return
        seen.add(asset.key)
        assets.append(asset)

    raw_message = message.get("raw_message") or []
    if isinstance(raw_message, list):
        for item in raw_message:
            if not isinstance(item, dict):
                continue
            itype = str(item.get("type") or "").strip().lower()
            data = item.get("data")

            if itype in {"video", "short_video", "video_file"}:
                if isinstance(data, str):
                    add(VideoAsset(url=data, name=Path(urlparse(data).path).name,
                                   source="video"))
                elif isinstance(data, dict):
                    add(_from_payload(data, source="video"))

            elif itype == "file":
                payload = data if isinstance(data, dict) else {}
                if _is_video_name(str(payload.get("name") or payload.get("file") or ""),
                                  str(payload.get("url") or "")):
                    add(_from_payload(payload, source="file"))

            elif itype == "text":
                text = data if isinstance(data, str) else str((data or {}).get("text") or "")
                for a in _from_text(text):
                    add(a)

    # 兜底：processed_plain_text 里可能只剩文本占位
    plain = str(message.get("processed_plain_text") or "")
    if plain:
        for a in _from_text(plain):
            add(a)

    return assets


def _from_payload(payload: dict[str, Any], *, source: str) -> VideoAsset | None:
    name = str(payload.get("name") or payload.get("file") or payload.get("file_name") or "").strip()
    url = str(payload.get("url") or payload.get("file_url") or "").strip()
    local_path = str(payload.get("local_path") or payload.get("file_path") or "").strip()
    base64_data = str(payload.get("base64") or "").strip()
    file_ref = str(payload.get("file_id") or "").strip()
    if not file_ref and name and not url:
        file_ref = name  # NapCat 占位里只有文件名时，用它去 get_file
    if not (name or url or local_path or base64_data or file_ref):
        return None
    return VideoAsset(name=name, file_ref=file_ref, url=url,
                      local_path=local_path, base64_data=base64_data, source=source)


def _from_text(text: str) -> list[VideoAsset]:
    out: list[VideoAsset] = []
    for m in _RE_VIDEO.finditer(str(text or "")):
        name = _clean_name(m.group("name"))
        if name and not name.lower().startswith("http"):
            out.append(VideoAsset(name=name, file_ref=name, source="text"))
    for m in _RE_FILE.finditer(str(text or "")):
        name = _clean_name(m.group("name"))
        if _is_video_name(name):
            out.append(VideoAsset(name=name, file_ref=name, source="text"))
    return out


# ---------------- 落盘 ----------------

def _is_public_url(url: str) -> bool:
    """解析 URL 并确认主机解析到的地址均为公网地址。"""

    try:
        parsed = urlparse(url)
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname
                or parsed.username is not None or parsed.password is not None):
            return False
        explicit_port = parsed.port
        port = explicit_port if explicit_port is not None else (
            443 if parsed.scheme == "https" else 80)
        _resolve_addresses(parsed.hostname, port)
    except (OSError, TypeError, ValueError):
        return False
    return True


def _resolve_addresses(host: str, port: int, *, allow_private: bool = False) -> list[str]:
    """解析一次并返回将要直连的 IP，避免校验与连接之间的 DNS 重绑定。"""

    try:
        records = socket.getaddrinfo(
            host, port, type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP)
    except OSError:
        raise ValueError("无法解析视频地址") from None
    addresses = list(dict.fromkeys(record[4][0].split("%", 1)[0]
                                   for record in records))
    if not addresses:
        raise ValueError("视频地址没有可用的网络地址")
    if not allow_private:
        try:
            public = [ipaddress.ip_address(address).is_global for address in addresses]
        except ValueError:
            raise ValueError("视频地址解析失败") from None
        if not all(public):
            raise ValueError("拒绝下载非公网地址")
    return addresses


class _PinnedHTTPConnection(http.client.HTTPConnection):
    """连接到已校验的 IP，同时保留原始 Host 头。"""

    def __init__(self, host: str, port: int, address: str, timeout_s: float):
        super().__init__(host, port, timeout=timeout_s)
        self._address = address

    def connect(self) -> None:
        self.sock = socket.create_connection((self._address, self.port), self.timeout)


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """直连已校验的 IP，并用原始主机名验证 TLS 证书与 SNI。"""

    def __init__(self, host: str, port: int, address: str, timeout_s: float):
        super().__init__(host, port, timeout=timeout_s, context=ssl.create_default_context())
        self._address = address

    def connect(self) -> None:
        raw = socket.create_connection((self._address, self.port), self.timeout)
        try:
            self.sock = self._context.wrap_socket(raw, server_hostname=self.host)
        except Exception:
            raw.close()
            raise


def _download_sync(url: str, *, timeout_s: float, max_bytes: int,
                   allow_private: bool = False) -> bytes:
    current_url = url
    previous_scheme = ""
    redirect_statuses = {301, 302, 303, 307, 308}

    # 最多跟随五次重定向；每一跳都重新解析、校验并固定目标 IP。
    for redirect_count in range(6):
        try:
            parsed = urlparse(current_url)
            scheme = parsed.scheme.lower()
            host = parsed.hostname or ""
            if (scheme not in {"http", "https"} or not host
                    or parsed.username is not None or parsed.password is not None):
                raise ValueError("视频地址格式无效")
            if previous_scheme == "https" and scheme != "https":
                raise ValueError("拒绝不安全的视频地址跳转")
            explicit_port = parsed.port
            port = explicit_port if explicit_port is not None else (
                443 if scheme == "https" else 80)
            if not 1 <= port <= 65535:
                raise ValueError("视频地址端口无效")
            addresses = _resolve_addresses(host, port, allow_private=allow_private)
            host_header = f"[{host}]" if ":" in host else host
            if explicit_port is not None:
                host_header = f"{host_header}:{port}"
            request_target = parsed.path or "/"
            if parsed.query:
                request_target = f"{request_target}?{parsed.query}"
        except ValueError:
            raise
        except Exception:
            raise ValueError("视频地址格式无效") from None

        redirect_url = ""
        for address in addresses:
            if scheme == "https":
                conn = _PinnedHTTPSConnection(host, port, address, timeout_s)
            else:
                conn = _PinnedHTTPConnection(host, port, address, timeout_s)
            try:
                conn.request("GET", request_target, headers={
                    "Host": host_header,
                    "User-Agent": "maivideo/0.1",
                    "Accept": "*/*",
                    "Connection": "close",
                })
                response = conn.getresponse()
                if response.status in redirect_statuses:
                    location = response.getheader("Location")
                    if not location:
                        raise ValueError("视频地址返回了无目标的重定向")
                    redirect_url = urljoin(current_url, location)
                    break
                if not 200 <= response.status < 300:
                    raise ValueError(f"视频下载返回 HTTP {response.status}")
                data = response.read(max_bytes + 1)
                if len(data) > max_bytes:
                    raise ValueError(f"下载内容过大：{len(data)} > {max_bytes}")
                return data
            except ValueError:
                raise
            except (OSError, http.client.HTTPException, ssl.SSLError):
                # 不把底层异常写入日志，避免泄露 URL 中的签名查询参数。
                continue
            finally:
                conn.close()

        if redirect_url:
            if redirect_count == 5:
                raise ValueError("视频地址重定向次数过多")
            previous_scheme = scheme
            current_url = redirect_url
            continue
        raise ValueError("视频下载网络请求失败") from None

    raise ValueError("视频地址重定向次数过多")


async def materialize(asset: VideoAsset, *, target_dir: Path, timeout_s: float,
                      max_bytes: int, allow_private: bool = False) -> Path:
    """把素材落到本地文件，返回路径。"""

    target_dir.mkdir(parents=True, exist_ok=True)
    ext = Path(asset.name or asset.url or "x.mp4").suffix.lower() or ".mp4"
    out = target_dir / f"{asset.key[:16]}{ext}"

    if asset.local_path:
        src = Path(asset.local_path)
        if src.is_file():
            if src.stat().st_size > max_bytes:
                raise ValueError("本地视频过大")
            if src.resolve() != out.resolve():
                await asyncio.to_thread(_copy, src, out)
            return out

    if asset.base64_data:
        raw = base64.b64decode(asset.base64_data.split(",")[-1], validate=False)
        if len(raw) > max_bytes:
            raise ValueError("base64 视频过大")
        await asyncio.to_thread(out.write_bytes, raw)
        return out

    if asset.url:
        raw = await asyncio.to_thread(
            _download_sync, asset.url,
            timeout_s=timeout_s, max_bytes=max_bytes, allow_private=allow_private)
        await asyncio.to_thread(out.write_bytes, raw)
        return out

    raise ValueError("素材缺少 url / base64 / local_path")


def _copy(src: Path, dst: Path) -> None:
    import shutil
    shutil.copy2(src, dst)


def save_bytes(raw: bytes, *, target_dir: Path, name: str, key: str) -> Path:
    """把已取回的字节写盘。"""

    target_dir.mkdir(parents=True, exist_ok=True)
    ext = Path(name or "x.mp4").suffix.lower() or ".mp4"
    out = target_dir / f"{key[:16]}{ext}"
    out.write_bytes(raw)
    return out
