# -*- coding: utf-8 -*-
"""下载器 SSRF 与签名 URL 日志边界测试；全程使用桩，不发起外网请求。"""
import socket
import sys
from unittest.mock import patch

ROOT = __import__("pathlib").Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import media as M  # noqa: E402

ok = fail = 0


def check(name, condition, extra=""):
    global ok, fail
    if condition:
        ok += 1
        print(f"  [PASS] {name} {extra}")
    else:
        fail += 1
        print(f"  [FAIL] {name} {extra}")


def _record(address, port):
    return (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP,
            "", (address, port))


def _response(status, location="", body=b""):
    class Response:
        def __init__(self):
            self.status = status

        def getheader(self, name):
            return location if name.lower() == "location" else None

        def read(self, _limit):
            return body

    return Response()


print("[1] 解析失败或含非公网地址时必须拒绝")
with patch.object(M.socket, "getaddrinfo", side_effect=socket.gaierror):
    check("DNS 失败关闭", not M._is_public_url("http://missing.test/video.mp4"))

with patch.object(M.socket, "getaddrinfo",
                  return_value=[_record("127.0.0.1", 80)]):
    check("环回地址拒绝", not M._is_public_url("http://private.test/video.mp4"))

with patch.object(M.socket, "getaddrinfo", return_value=[
        _record("8.8.8.8", 80), _record("10.0.0.7", 80)]):
    check("混合公网与内网 DNS 答案整体拒绝",
          not M._is_public_url("http://mixed.test/video.mp4"))

print("\n[2] 连接固定到已校验的 IP")
connections = []


class _BodyConnection:
    def __init__(self, host, port, address, timeout_s):
        self.address = address
        connections.append(self)

    def request(self, *_args, **_kwargs):
        pass

    def getresponse(self):
        return _response(200, body=b"video-data")

    def close(self):
        pass


def _public_dns(host, port, **_kwargs):
    return [_record("8.8.8.8", port)]

with patch.object(M.socket, "getaddrinfo", side_effect=_public_dns), \
        patch.object(M, "_PinnedHTTPConnection", _BodyConnection):
    body = M._download_sync(
        "http://public.test/video.mp4?fixture=redacted",
        timeout_s=2, max_bytes=100)
check("下载内容正确", body == b"video-data")
check("实际连接使用通过校验的解析结果",
      len(connections) == 1 and connections[0].address == "8.8.8.8")

print("\n[3] 每次重定向都重新校验，且错误不泄露签名参数")
redirect_connections = []


class _RedirectConnection:
    def __init__(self, host, port, address, timeout_s):
        redirect_connections.append((host, address))

    def request(self, *_args, **_kwargs):
        pass

    def getresponse(self):
        return _response(
            302, location="http://private.test/internal?fixture=redacted")

    def close(self):
        pass


def _redirect_dns(host, port, **_kwargs):
    address = "8.8.8.8" if host == "public.test" else "10.0.0.7"
    return [_record(address, port)]

with patch.object(M.socket, "getaddrinfo", side_effect=_redirect_dns), \
        patch.object(M, "_PinnedHTTPConnection", _RedirectConnection):
    try:
        M._download_sync(
            "http://public.test/video.mp4?fixture=redacted",
            timeout_s=2, max_bytes=100)
    except ValueError as exc:
        redirect_error = str(exc)
    else:
        redirect_error = ""
check("私网重定向目标被拒绝", "拒绝下载非公网地址" in redirect_error)
check("拒绝前只连接已校验的公网主机", redirect_connections == [
    ("public.test", "8.8.8.8")])
check("失败文本不含查询参数", "fixture" not in redirect_error and
      "redacted" not in redirect_error)

print(f"\n结果：{ok} 通过 / {fail} 失败")
sys.exit(1 if fail else 0)
