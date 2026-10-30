"""上传请求组装与响应判定的自检。

直接运行：python tests/test_upload_api.py
"""
import os
import sys
from typing import get_args

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from plugin import JMPluginConfig, build_upload_request, parse_upload_response


def test_group_header_auth() -> None:
    """群聊 + header 鉴权：默认路径不做转换。"""
    url, headers, params, payload = build_upload_request(
        "group", 123, r"E:\tmp\JM_1.pdf", "JM_1.pdf", "http://127.0.0.1:3000", "tok",
    )
    assert url == "http://127.0.0.1:3000/upload_group_file"
    assert headers == {"Authorization": "Bearer tok"} and params == {}
    assert payload == {"group_id": 123, "file": r"E:\tmp\JM_1.pdf", "name": "JM_1.pdf"}


def test_private_query_auth_and_wsl() -> None:
    """私聊 + query 鉴权（SnowLuma）+ WSL 路径转换。"""
    url, headers, params, payload = build_upload_request(
        "private", 456, r"E:\tmp\JM_1.dat", "JM_1.dat", "http://127.0.0.1:3000", "tok",
        auth_mode="query", enable_wsl_path=True,
    )
    assert url.endswith("/upload_private_file")
    assert payload["user_id"] == 456 and payload["file"] == "/mnt/e/tmp/JM_1.dat"
    assert params == {"access_token": "tok"} and headers == {}


def test_no_token() -> None:
    """不配置 Token 时不带任何鉴权信息。"""
    _, headers, params, _ = build_upload_request("group", 1, "x.pdf", "x.pdf", "http://h")
    assert headers == {} and params == {}


def test_parse_success_variants() -> None:
    """成功：标准响应，以及字段缺失（文档里全部为可选）的响应。"""
    assert parse_upload_response(200, '{"status":"ok","retcode":0,"data":{"file_id":"f"}}')[0]
    assert parse_upload_response(200, '{"data":{"file_id":"f"}}')[0]
    assert parse_upload_response(200, "ok")[0]


def test_parse_failures() -> None:
    """失败：200 + status failed、200 + retcode 非 0、非 200。"""
    ok, msg = parse_upload_response(200, '{"status":"failed","retcode":-1,"message":"无权限"}')
    assert not ok and "无权限" in msg
    assert not parse_upload_response(200, '{"retcode":1404}')[0]
    assert not parse_upload_response(500, "boom")[0]


def test_config_dropdowns() -> None:
    """framework / auth_mode 是 Literal，WebUI 才会渲染成下拉框。"""
    jm = JMPluginConfig().jm
    assert jm.framework == "snowluma" and jm.auth_mode == "header"
    fields = type(jm).model_fields
    assert set(get_args(fields["framework"].annotation)) == {"snowluma", "napcat"}
    assert set(get_args(fields["auth_mode"].annotation)) == {"header", "query"}


if __name__ == "__main__":
    for name, func in list(globals().items()):
        if name.startswith("test_") and callable(func):
            func()
            print(f"ok  {name}")
    print("全部通过")