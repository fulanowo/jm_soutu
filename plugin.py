# plugin.py
"""
JM 本子下载插件

概述：
- 通过命令下载指定 ID 的 JMComic 本子
- 支持多章节本子的全部下载或指定章节下载
- 自动合成 PDF 并加密压缩上传到 QQ 群或私聊（Napcat）

作者：yumemi1
项目：https://github.com/yumemi1/jm_plugin
"""
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import asyncio
import base64
import json
import os
import re
import shutil
import zipfile

import aiohttp
from PIL import Image

try:
    from curl_cffi.requests import AsyncSession  # noqa: F401
    HAS_CURL_CFFI = True
except ImportError:
    HAS_CURL_CFFI = False

try:
    import pyzipper
    HAS_PYZIPPER = True
except ImportError:
    HAS_PYZIPPER = False

import logging

from maibot_sdk import Command, Field, MaiBotPlugin, PluginConfigBase

logger = logging.getLogger(__name__)


# =============================================================================
# 配置模型
# =============================================================================

class PluginSectionConfig(PluginConfigBase):
    """插件基础配置节。"""

    __ui_label__ = "插件设置"
    __ui_icon__ = "settings"
    __ui_order__ = 0

    config_version: str = Field(default="1.0.0", description="配置文件版本号")
    enabled: bool = Field(default=True, description="是否启用插件")


class JMSectionConfig(PluginConfigBase):
    """JM 下载相关配置节。"""

    __ui_label__ = "下载设置"
    __ui_icon__ = "download"
    __ui_order__ = 1

    jm_data_dir: str = Field(default="", description="JM 漫画下载目录，为空则使用 data/plugins/yumemi1.jm_plugin/")
    owner_qq: str = Field(default="", description="主人QQ号，只有此QQ号才能使用下载命令（为空则不限制）")
    napcat_base_url: str = Field(default="http://127.0.0.1:3000", description="Napcat 机器人框架的 API 基础 URL")
    napcat_token: str = Field(default="", description="Napcat API 鉴权 Token（留空则不发送）")
    enable_wsl_path: bool = Field(default=False, description="是否将文件路径转换为 WSL 路径（Napcat 运行在 WSL 中时需启用）")
    max_pdf_pages: int = Field(default=300, description="PDF 最大页数限制")
    use_encrypted_zip: bool = Field(default=False, description="是否生成加密压缩包（密码为文件名）并伪装格式")
    fake_extension: str = Field(default=".dat", description="压缩包伪装的文件扩展名（如 .dat, .bin, .tmp 等）")


class SotuSectionConfig(PluginConfigBase):
    """SauceNAO 搜图配置节。"""

    __ui_label__ = "搜图设置"
    __ui_icon__ = "image"
    __ui_order__ = 2

    api_key: str = Field(default="1115d151d73f404c1522bdf28542a50b42c27192", description="SauceNAO API Key (必填，否则会有搜索限制)")
    proxy: str = Field(default="", description="搜图使用的代理地址 (http://... 或 socks5://...)，为空则直连")


class JMPluginConfig(PluginConfigBase):
    """JM 插件聚合配置。"""

    plugin: PluginSectionConfig = Field(default_factory=PluginSectionConfig)
    jm: JMSectionConfig = Field(default_factory=JMSectionConfig)
    sotu: SotuSectionConfig = Field(default_factory=SotuSectionConfig)


# =============================================================================
# 工具函数
# =============================================================================

def sanitize_filename(name: str) -> str:
    """将文件名中的非法字符替换为下划线。

    替换字符包括：\\ / : * ? " < > | 以及换行符。

    Args:
        name: 原始文件名

    Returns:
        处理后的安全文件名
    """
    return re.sub(r'[\\/:*?"<>|\r\n]+', '_', name).strip()


def images_to_pdf_sync(image_paths: List[Path], output_pdf_path: str) -> str:
    """按顺序将图片合并为单个 PDF。

    - 所有图片统一转换为 RGB 模式。
    - 自动创建输出目录（若不存在）。

    Args:
        image_paths: 图片文件路径列表
        output_pdf_path: 输出 PDF 文件路径

    Returns:
        生成的 PDF 文件路径

    Raises:
        ValueError: 当没有图片提供时
    """
    if not image_paths:
        raise ValueError("没有图片可合并")

    images: List[Image.Image] = []
    # 使用上下文管理器确保文件句柄及时释放
    for img_path in image_paths:
        with Image.open(img_path) as im:
            img = im.convert("RGB") if im.mode != "RGB" else im.copy()
        images.append(img)

    os.makedirs(os.path.dirname(output_pdf_path), exist_ok=True)

    first_image = images[0]
    remaining_images = images[1:]
    first_image.save(output_pdf_path, save_all=True, append_images=remaining_images)

    for img in images:
        try:
            img.close()
        except Exception:
            pass

    return output_pdf_path


def pdf_to_encrypted_zip(pdf_path: str, zip_path: str, password: str) -> str:
    """将 PDF 文件转换为加密的 ZIP 压缩包（使用 pyzipper）。

    Args:
        pdf_path: PDF 文件路径
        zip_path: 输出的 ZIP 文件路径
        password: 压缩包密码

    Returns:
        生成的 ZIP 文件路径

    Raises:
        FileNotFoundError: 当 PDF 文件不存在时
        Exception: 压缩失败时
    """
    if not os.path.exists(pdf_path):
        raise FileNotFoundError(f"PDF 文件不存在: {pdf_path}")

    os.makedirs(os.path.dirname(zip_path), exist_ok=True)

    pdf_filename = os.path.basename(pdf_path)

    try:
        if HAS_PYZIPPER:
            # 使用 pyzipper 创建加密 ZIP
            with pyzipper.AESZipFile(zip_path, 'w', compression=pyzipper.ZIP_DEFLATED, encryption=pyzipper.WZ_AES) as zf:
                zf.setpassword(password.encode('utf-8'))
                zf.write(pdf_path, arcname=pdf_filename)
        else:
            # 如果没有 pyzipper，使用普通 zipfile（不加密）
            with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:
                zf.write(pdf_path, arcname=pdf_filename)
        return zip_path
    except Exception as e:
        raise Exception(f"压缩失败: {e}") from e


# =============================================================================
# Napcat API 交互
# =============================================================================

def _convert_to_wsl_path(windows_path: str) -> str:
    """将 Windows 绝对路径转换为 WSL 可访问的路径（/mnt/...）。

    Args:
        windows_path: Windows 路径，如 E:\\MaiM-with-u\\...\\file.dat

    Returns:
        WSL 路径，如 /mnt/e/MaiM-with-u/.../file.dat
    """
    if len(windows_path) >= 2 and windows_path[1] == ':':
        drive = windows_path[0].lower()
        rest = windows_path[2:].replace('\\', '/')
        return f'/mnt/{drive}{rest}'
    return windows_path.replace('\\', '/')


async def upload_pdf_via_napcat(
    pdf_path: str,
    filename: str,
    scope: str,
    target_id: int,
    napcat_base: str,
    token: str = "",
    enable_wsl_path: bool = False,
    timeout: int = 60,
) -> Tuple[bool, str]:
    """通过 Napcat API 上传 PDF 文件。

    尝试两种方式上传：
    1) JSON：传本地文件路径；失败则回退
    2) FormData：上传二进制内容

    Args:
        pdf_path: PDF 文件本地路径
        filename: 上传时的文件名
        scope: "group" 群聊 或 "private" 私聊
        target_id: 群号或用户 ID
        napcat_base: Napcat API 基础 URL
        token: Napcat API 鉴权 Token（为空时不发送）
        enable_wsl_path: 是否将 Windows 路径转换为 WSL 路径
        timeout: 请求总超时（秒）

    Returns:
        (是否成功, 响应文本或错误信息)
    """
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    file_path_for_server = _convert_to_wsl_path(pdf_path) if enable_wsl_path else pdf_path

    if scope == "group":
        url = f"{napcat_base}/upload_group_file"
        json_payload = {"group_id": target_id, "file": file_path_for_server, "name": filename}
    else:
        url = f"{napcat_base}/upload_private_file"
        json_payload = {"user_id": target_id, "file": file_path_for_server, "name": filename}

    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)) as sess:
        # 方式 1：JSON 方式（直接传递文件路径）
        try:
            async with sess.post(url, json=json_payload, headers=headers) as response:
                response_text = await response.text()
                if response.status == 200:
                    try:
                        # 尝试格式化 JSON 响应
                        return True, json.dumps(json.loads(response_text), ensure_ascii=False)
                    except Exception:
                        return True, response_text
        except Exception:
            pass  # 静默失败，继续尝试方式 2

        # 方式 2：FormData 方式（上传文件二进制）
        form = aiohttp.FormData()
        if scope == "group":
            form.add_field("group_id", str(target_id))
        else:
            form.add_field("user_id", str(target_id))

        if token:
            form.add_field("access_token", token)

        form.add_field("name", filename)
        file_handle = open(pdf_path, "rb")
        form.add_field("file", file_handle, filename=filename, content_type="application/pdf")

        try:
            async with sess.post(url, data=form, headers=headers) as response:
                response_text = await response.text()
                if response.status == 200:
                    try:
                        return True, json.dumps(json.loads(response_text), ensure_ascii=False)
                    except Exception:
                        return True, response_text
                else:
                    return False, f"HTTP {response.status}: {response_text}"
        finally:
            file_handle.close()


# =============================================================================
# 搜图与图片提取相关
# =============================================================================

def _extract_image_from_message(message_dict: Optional[Dict[str, Any]]) -> Optional[str]:
    """从消息字典的 raw_message 段中提取图片 base64 数据。

    Args:
        message_dict: 消息字典（message.get_recent / get_by_id 返回）

    Returns:
        image 段的 binary_data_base64；未找到时返回 None
    """
    if not isinstance(message_dict, dict):
        return None
    raw = message_dict.get("raw_message") or []
    for seg in raw:
        if isinstance(seg, dict) and seg.get("type") == "image":
            image_b64 = seg.get("binary_data_base64")
            if image_b64:
                return image_b64
    return None


def _get_image_segment(message_dict: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """从消息字典中提取第一个 image 段。

    Args:
        message_dict: 消息字典

    Returns:
        image 段字典；未找到时返回 None
    """
    if not isinstance(message_dict, dict):
        return None
    raw = message_dict.get("raw_message") or []
    for seg in raw:
        if isinstance(seg, dict) and seg.get("type") == "image":
            return seg
    return None


async def search_image_by_saucenao(image_path: str, proxy: Optional[str] = None, api_key: Optional[str] = None) -> Tuple[bool, Any]:
    """使用 SauceNAO 搜索图片

    Returns:
        (是否成功, 结果字典或错误信息)
    """
    url = "https://saucenao.com/search.php"

    # 构造参数
    params = {
        "db": 999,          # 搜索所有数据库
        "output_type": 2,   # JSON 格式
        "testmode": 1,
        "numres": 5,
    }
    if api_key:
        params["api_key"] = api_key

    # 构造 Headers (伪装成 Chrome)
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    }

    # 方案 A: 使用 curl_cffi (推荐)
    if HAS_CURL_CFFI:
        try:
            from curl_cffi.requests import AsyncSession
            from curl_cffi import CurlMime

            # 使用 CurlMime 构造 multipart/form-data
            mp = CurlMime()
            mp.addpart(
                name="file",
                content_type="application/octet-stream",
                filename="image.jpg",
                local_path=image_path
            )

            # 策略 1: 模拟浏览器 (带完整 Headers)
            try:
                async with AsyncSession(impersonate="chrome110") as session:
                    response = await session.post(
                        url,
                        params=params,
                        headers=headers, # 使用之前定义的浏览器 headers
                        multipart=mp,    # 使用 multipart 参数
                        proxy=proxy,
                        timeout=30
                    )
                    if response.status_code == 200:
                        mp.close() # 记得关闭
                        return True, response.json()
            except Exception:
                pass

            # 重置 CurlMime (需要重新创建，因为内部指针可能已移动)
            mp.close()
            mp = CurlMime()
            mp.addpart(
                name="file",
                content_type="application/octet-stream",
                filename="image.jpg",
                local_path=image_path
            )

            # 策略 2: 纯 API 调用 (不模拟浏览器，不带 User-Agent 或带默认的)
            try:
                async with AsyncSession() as session:
                    response = await session.post(
                        url,
                        params=params,
                        multipart=mp, # 使用 multipart 参数
                        proxy=proxy,
                        timeout=30
                    )
                    mp.close()
                    if response.status_code == 200:
                        return True, response.json()
                    elif response.status_code == 403:
                        return False, "403 Forbidden (API Key 可能无效或 IP 被 ban)"
                    else:
                        return False, f"HTTP {response.status_code}"
            except Exception as e:
                mp.close()
                return False, f"curl_cffi 异常: {e}"

        except Exception as e:
            return False, f"curl_cffi 导入/执行异常: {e}"

    # 方案 B: 使用 aiohttp (回退)
    if not aiohttp:
        return False, "依赖缺失: aiohttp"

    try:
        async with aiohttp.ClientSession() as session:
            with open(image_path, 'rb') as f:
                form = aiohttp.FormData()
                form.add_field('file', f, filename='image.jpg', content_type='application/octet-stream')

                # aiohttp 需要手动拼接 params 到 url 或者传 params 参数
                async with session.post(url, params=params, data=form, headers=headers, proxy=proxy, timeout=aiohttp.ClientTimeout(total=60)) as response:
                    if response.status == 200:
                        try:
                            return True, await response.json()
                        except Exception as e:
                            return False, f"JSON解析失败: {e}"
                    elif response.status == 403:
                        return False, "403 Forbidden (Cloudflare 拦截)"
                    elif response.status == 429:
                        return False, "429 Too Many Requests"
                    else:
                        return False, f"HTTP {response.status}"
    except Exception as e:
        return False, f"aiohttp 请求异常: {e}"


async def search_jm_by_keyword(keyword: str, option, page: int = 1) -> List[Dict]:
    """在 JMComic 中搜索关键字"""
    try:
        import jmcomic  # noqa: F401
        # 避免阻塞
        def _search():
            client = option.new_jm_client()
            # 搜索指定页
            try:
                # 修正：使用 search_site 而不是 search_album
                # JMComic-Crawler-Python 库中，全站搜索的方法名是 search_site
                page_result = client.search_site(keyword, page=page)
                return page_result
            except Exception as e:
                logger.error(f"JM Search Error: {e}")
                return []

        results = await asyncio.to_thread(_search)

        # 处理可能的 None 返回
        if not results:
            return []

        data = []
        # 优先尝试遍历 content 属性 (JmSearchPage)
        if hasattr(results, 'content'):
            for album_id, album_info in results.content:
                if isinstance(album_info, dict):
                    title = album_info.get('name', album_info.get('title', '未知'))
                    author = album_info.get('author', '未知')
                    if isinstance(author, list):
                        author = ','.join(author)
                    likes = album_info.get('likes', '0')

                    data.append({
                        "id": album_id,
                        "title": title,
                        "author": author,
                        "likes": likes
                    })
                else:
                    # 如果 content 里不是 (id, dict) 结构，做个兼容
                    pass

        # 如果 data 为空 (可能 results 不是 JmSearchPage 或者 content 为空)，尝试直接遍历 results
        if not data:
            for album in results:
                if hasattr(album, 'album_id'):
                    data.append({
                        "id": album.album_id,
                        "title": album.title,
                        "author": getattr(album, 'author', '未知'),
                        "likes": getattr(album, 'likes', '0')
                    })
                elif isinstance(album, tuple) and len(album) >= 2:
                    data.append({
                        "id": album[0],
                        "title": album[1],
                        "author": '未知',
                        "likes": '0'
                    })

        return data
    except Exception as e:
        logger.error(f"Search Wrapper Error: {e}")
        return []


# =============================================================================
# JMComic 下载功能
# =============================================================================

async def check_album_chapters(
    album_id: str,
    output_dir: Optional[str] = None,
) -> Tuple[bool, Optional[int], Optional[str]]:
    """检查本子的章节数量。

    初始化 jmcomic 配置后查询指定 ID 的章节信息。

    Args:
        album_id: 本子 ID
        output_dir: 自定义输出目录，为空时使用默认目录

    Returns:
        元组 (是否成功, 章节数量, 错误信息)
        - 成功时返回 (True, 章节数, None)
        - 失败时返回 (False, None, 错误信息)
    """
    try:
        import jmcomic
    except Exception as e:
        return False, None, f"jmcomic 导入失败: {e}"

    try:
        if output_dir is None:
            output_dir = os.path.join(os.getcwd(), "data")

        output_dir = os.path.abspath(output_dir)
        os.makedirs(output_dir, exist_ok=True)

        option_file = os.path.join(output_dir, "option.yml")
        with open(option_file, "w", encoding="utf-8") as config_file:
            config_file.write(f"dir_rule:\n  base_dir: {output_dir}\n")
            # 限制并发数以防止服务器卡死 (默认30+线程会导致资源耗尽)
            config_file.write("download:\n  threading:\n    image: 5\n")

        option = jmcomic.create_option_by_file(option_file)

        # 使用线程池避免阻塞异步事件循环
        def get_album_info():
            client = option.new_jm_client()
            album = client.get_album_detail(album_id)
            return len(album)

        chapter_count = await asyncio.to_thread(get_album_info)
        return True, chapter_count, None
    except Exception as e:
        return False, None, f"获取章节信息失败: {e}"


async def async_download_album(
    album_id: str,
    output_dir: Optional[str] = None,
    only_first_chapter: bool = False,
    chapter_index: Optional[int] = None,
    chapter_range: Optional[Tuple[int, int]] = None,
) -> Tuple[bool, Optional[str], Optional[int]]:
    """异步下载 JMComic 本子。

    支持四种下载模式：
    1. 下载整本（only_first_chapter=False 且未指定 chapter_index 和 chapter_range）
    2. 仅下载第一章（only_first_chapter=True）
    3. 下载指定章节（指定 chapter_index，1-based）
    4. 下载章节范围（指定 chapter_range，如 (1, 5) 表示下载第1到5章）

    Args:
        album_id: 本子 ID
        output_dir: 自定义输出目录，为空时使用默认目录
        only_first_chapter: 是否仅下载第一章
        chapter_index: 指定章节号（从 1 开始），优先级高于 only_first_chapter
        chapter_range: 指定章节范围（起始章节, 结束章节），优先级最高

    Returns:
        (是否成功, 图片目录或错误信息, 总章节数或 None)
    """
    try:
        import jmcomic
    except Exception as e:
        return False, f"jmcomic 导入失败: {e}", None

    # 确定下载目录
    if output_dir is None:
        output_dir = os.path.join(os.getcwd(), "data")

    output_dir = os.path.abspath(output_dir)
    os.makedirs(output_dir, exist_ok=True)

    option_file = os.path.join(output_dir, "option.yml")
    with open(option_file, "w", encoding="utf-8") as config_file:
        config_file.write(f"dir_rule:\n  base_dir: {output_dir}\n")
        # 限制并发数以防止服务器卡死 (默认30+线程会导致资源耗尽)
        config_file.write("download:\n  threading:\n    image: 5\n")

    try:
        option = jmcomic.create_option_by_file(option_file)
    except Exception as e:
        return False, f"jmcomic 配置错误: {e}", None

    # 解析并确定要下载的章节
    photo_id_to_download = None
    photo_ids_to_download = None
    total_chapters = None
    download_chapter_index = None

    if chapter_range is not None or chapter_index is not None or only_first_chapter:
        try:
            def get_album_chapters():
                client = option.new_jm_client()
                album = client.get_album_detail(album_id)
                total = len(album)
                return list(album), total

            chapters_list, total_chapters = await asyncio.to_thread(get_album_chapters)

            if total_chapters == 0:
                return False, "本子无章节信息", None

            if chapter_range is not None:
                # 下载章节范围
                start_chapter, end_chapter = chapter_range
                photo_ids_to_download = []
                for i in range(start_chapter - 1, min(end_chapter, total_chapters)):
                    photo_id = chapters_list[i].photo_id
                    if photo_id is not None:
                        photo_ids_to_download.append(photo_id)
            elif chapter_index is not None:
                # 用户指定了章节号（1-based），转换为内部索引（0-based）
                idx = chapter_index - 1
                if idx < 0 or idx >= total_chapters:
                    download_chapter_index = 0 if only_first_chapter else None
                else:
                    download_chapter_index = idx

                if download_chapter_index is not None:
                    photo_id_to_download = chapters_list[download_chapter_index].photo_id
                    if photo_id_to_download is None:
                        return False, "无法获取指定章节信息", None
            else:
                download_chapter_index = 0
                photo_id_to_download = chapters_list[download_chapter_index].photo_id
                if photo_id_to_download is None:
                    return False, "无法获取指定章节信息", None
        except Exception as e:
            return False, f"获取章节信息失败: {e}", None

    try:
        if photo_ids_to_download is not None:
            # 下载多个章节
            for photo_id in photo_ids_to_download:
                await asyncio.to_thread(jmcomic.download_photo, photo_id, option)
        elif download_chapter_index is not None:
            await asyncio.to_thread(jmcomic.download_photo, photo_id_to_download, option)
        else:
            await asyncio.to_thread(jmcomic.download_album, album_id, option)
    except Exception as e:
        return False, f"下载失败: {e}", None

    # 定位下载的图片目录
    # jmcomic 会在输出目录下创建子目录，需要找到包含图片的目录
    subdirs = [d for d in Path(output_dir).iterdir() if d.is_dir()]
    if not subdirs:
        return False, "未找到下载目录", None

    subdirs.sort(key=lambda d: d.stat().st_mtime, reverse=True)

    target_dir = None
    image_extensions = [".jpg", ".jpeg", ".png", ".webp"]
    for subdir in subdirs:
        images = []
        for ext in image_extensions:
            images.extend(subdir.rglob(f"*{ext}"))
        if images:
            target_dir = str(subdir)
            break

    if not target_dir:
        return False, "未找到图片目录", None

    return True, target_dir, total_chapters


# =============================================================================
# 插件主类
# =============================================================================

class JMPlugin(MaiBotPlugin):
    """JM 漫画下载插件主类。

    本插件为 MaiBot 提供 JMComic 图集下载功能。
    通过集成 jmcomic 库实现图集爬取，使用 Pillow 进行 PDF 转换，
    最后通过 Napcat API 将文件上传到 QQ。

    主要特性：
    - 支持单章节和多章节漫画
    - 可选择性下载指定章节
    - 自动 PDF 转换和页数限制
    - 支持群聊和私聊上传

    Python 依赖：
    - jmcomic: JMComic 爬虫库
    - pillow: 图片处理和 PDF 生成
    - aiohttp: HTTP 客户端
    """

    config_model = JMPluginConfig

    async def on_load(self) -> None:
        """插件加载完成后的回调：计算派生路径并创建目录。"""
        self._plugin_dir = os.path.dirname(os.path.abspath(__file__))
        self._jm_data_dir = self._resolve_jm_data_dir()
        os.makedirs(self._jm_data_dir, exist_ok=True)
        for tmp_name in ("tmp_pdf", "tmp_zip", "tmp_sotu"):
            os.makedirs(os.path.join(self._plugin_dir, tmp_name), exist_ok=True)
        self.ctx.logger.info("插件加载完成，数据目录: %s", self._jm_data_dir)

    async def on_unload(self) -> None:
        """插件卸载前的回调：清理临时目录残留文件。"""
        for tmp_name in ("tmp_pdf", "tmp_zip", "tmp_sotu"):
            tmp_dir = os.path.join(self._plugin_dir, tmp_name)
            if os.path.isdir(tmp_dir):
                shutil.rmtree(tmp_dir, ignore_errors=True)
                os.makedirs(tmp_dir, exist_ok=True)
        self.ctx.logger.info("插件已卸载")

    async def on_config_update(self, scope: str, config_data: dict, version: str) -> None:
        """处理配置热更新事件：检测 jm_data_dir 变化并刷新派生变量。"""
        new_data_dir = self._resolve_jm_data_dir()
        if new_data_dir != self._jm_data_dir:
            self._jm_data_dir = new_data_dir
            os.makedirs(self._jm_data_dir, exist_ok=True)
            self.ctx.logger.info("数据目录已变更: %s", self._jm_data_dir)
        self.ctx.logger.info("配置已更新: scope=%s, version=%s", scope, version)

    def _resolve_jm_data_dir(self) -> str:
        """解析 JM 数据目录：配置非空时优先，否则使用 ctx.paths.data_dir。"""
        configured = (self.config.jm.jm_data_dir or "").strip()
        if configured:
            return os.path.abspath(configured)
        return str(self.ctx.paths.data_dir)

    def _get_user_id(self, kwargs: Dict[str, Any]) -> str:
        """从命令 kwargs 中提取发送者用户 ID。"""
        return str(kwargs.get("user_id") or "")

    def _get_napcat_target(self, kwargs: Dict[str, Any]) -> Tuple[str, int]:
        """从消息中解析 Napcat 上传目标。

        Returns:
            (scope, target_id)：scope 为 "group" 或 "private"；无法识别时 ("", 0)
        """
        message = kwargs.get("message") or {}
        info = message.get("message_info") or {}
        group_info = info.get("group_info")
        if group_info and group_info.get("group_id"):
            try:
                return "group", int(group_info["group_id"])
            except (TypeError, ValueError):
                pass
        user_info = info.get("user_info") or {}
        if user_info.get("user_id"):
            try:
                return "private", int(user_info["user_id"])
            except (TypeError, ValueError):
                pass
        return "", 0

    async def _find_image_from_history(self, chat_id: str) -> Optional[str]:
        """从历史消息中查找最近的图片 base64 数据。

        使用 message.get_recent 能力（hours=1.0, limit=15, filter_mai=True,
        include_binary_data=True），与旧 message_api.get_recent_messages 行为一致。
        """
        if not chat_id:
            return None
        try:
            messages = await self.ctx.call_capability(
                "message.get_recent",
                chat_id=chat_id,
                hours=1.0,
                limit=15,
                filter_mai=True,
                include_binary_data=True,
            )
        except Exception as exc:
            self.ctx.logger.warning("获取历史消息失败: %s", exc)
            return None
        if not isinstance(messages, list):
            return None
        # Host 返回按时间正序排列，倒序遍历以命中最近的图片（与旧实现一致）
        for msg in reversed(messages):
            if isinstance(msg, dict):
                image_b64 = _extract_image_from_message(msg)
                if image_b64:
                    return image_b64
        return None

    async def _acquire_image(self, kwargs: Dict[str, Any]) -> Optional[str]:
        """按顺序获取搜图所需图片：当前消息 → get_by_id 兜底 → 1 小时历史。

        Returns:
            图片 base64 数据；未找到时返回 None
        """
        message = kwargs.get("message")
        segment = _get_image_segment(message)
        if segment:
            image_b64 = segment.get("binary_data_base64")
            if image_b64:
                return image_b64
            # 当前消息序列化不含二进制数据（include_binary_data=False），
            # 通过 get_by_id 重新获取完整消息取回图片数据
            message_id = (message or {}).get("message_id", "")
            if segment.get("hash") and message_id:
                try:
                    full_message = await self.ctx.message.get_by_id(
                        message_id, include_binary_data=True
                    )
                    image_b64 = _extract_image_from_message(full_message)
                    if image_b64:
                        return image_b64
                except Exception as exc:
                    self.ctx.logger.warning("通过 get_by_id 获取图片失败: %s", exc)

        # 回退：从 1 小时内的历史消息中查找图片
        return await self._find_image_from_history(kwargs.get("stream_id") or "")

    # ponytail: SDK 2.7.0 的 @Command 额外 kwargs 进入 metadata.metadata 嵌套层，
    # Host 只读组件声明顶层 timeout_ms（component_registry.py:118），
    # 此处传 timeout_ms 不生效，故不传；60s RPC 超时仅影响命令状态记录，
    # handler 内 ctx.send.text 走独立 cap.call 不受影响。
    @Command(
        "jm",
        description="下载或搜索 JM 本子。用法：/jm ID（多章节必须指定章节）或 /jm ID 章节号 或 /jm ID 起始-结束 或 /jm 关键词",
        pattern=r"^(?:@[^\s]+\s*)?/jm(?:\s+(?P<args>.+))?$",
    )
    async def handle_jm(self, **kwargs: Any) -> Tuple[bool, Optional[str], int]:
        """执行 /jm 命令主逻辑。

        功能：
        1. /jm 关键词 -> 搜索本子并返回列表
        2. /jm ID -> 下载指定本子
        3. /jm ID 章节数 -> 下载指定章节

        Returns:
            元组 (是否成功, 结果消息, 是否拦截)
        """
        stream_id = kwargs["stream_id"]
        matched = kwargs.get("matched_groups") or {}
        args_str = (matched.get("args") or "").strip()

        if not args_str:
            await self.ctx.send.text("用法：/jm ID 或 /jm ID 章节数", stream_id)
            return True, "用法提示", 2

        parts = args_str.split()
        album_id_or_keyword = parts[0]
        chapter_num = None
        search_page = 1

        # 判断是 ID 还是 关键词
        is_id = album_id_or_keyword.isdigit()

        if not is_id:
            # 如果是关键词，进行搜索

            # 尝试解析页码: /jm keyword page_num
            if len(parts) > 1 and parts[-1].isdigit():
                try:
                    search_page = int(parts[-1])
                    if search_page < 1:
                        search_page = 1
                    # 如果关键词包含空格，除了最后一个数字外都是关键词
                    album_id_or_keyword = " ".join(parts[:-1])
                except Exception:
                    pass
            elif len(parts) > 1:
                # 关键词本身包含空格
                album_id_or_keyword = args_str

            await self.ctx.send.text(f"正在搜索: {album_id_or_keyword} (第{search_page}页)", stream_id)

            try:
                import jmcomic
                option_file = os.path.join(self._jm_data_dir, "option.yml")

                # 读取配置中的代理（jm 无独立代理配置，沿用 sotu.proxy）
                proxy = self.config.sotu.proxy

                with open(option_file, "w", encoding="utf-8") as config_file:
                    config_content = f"dir_rule:\n  base_dir: {self._jm_data_dir}\n"
                    if proxy:
                        config_content += f"""
client:
  postman:
    meta_data:
      proxies:
        http: {proxy}
        https: {proxy}
"""
                    config_file.write(config_content)

                option = jmcomic.create_option_by_file(option_file)

                jm_results = await search_jm_by_keyword(album_id_or_keyword, option, page=search_page)

                if not jm_results:
                    await self.ctx.send.text("未找到相关本子", stream_id)
                    return True, "搜索无结果", 2

                display_count = 10
                msg = f"搜索结果 (第{search_page}页, 显示{min(len(jm_results), display_count)}个):\n"
                for i, album in enumerate(jm_results[:display_count], 1):
                    msg += f"{i}. ID: {album['id']} | {album['title']}\n"

                msg += "\n请输入 /jm ID 进行下载"
                msg += f"\n请输入 /jm {album_id_or_keyword} {search_page + 1} 查看下一页"

                await self.ctx.send.text(msg, stream_id)
                return True, "搜索结果已发送", 2

            except Exception as e:
                await self.ctx.send.text(f"搜索失败: {e}", stream_id)
                return True, f"搜索失败: {e}", 2

        # 如果是 ID，继续原有的下载逻辑
        album_id = album_id_or_keyword

        chapter_num = None
        chapter_range = None

        if len(parts) > 1:
            chapter_param = parts[1]

            # 检查是否是章节范围（如 1-5 或 1-）
            if '-' in chapter_param:
                try:
                    range_parts = chapter_param.split('-')
                    if len(range_parts) == 2:
                        start_chapter = int(range_parts[0]) if range_parts[0] else None
                        end_chapter = int(range_parts[1]) if range_parts[1] else None

                        if start_chapter is not None and start_chapter <= 0:
                            await self.ctx.send.text("起始章节必须为正整数", stream_id)
                            return True, "参数错误", 2

                        if end_chapter is not None and end_chapter <= 0:
                            await self.ctx.send.text("结束章节必须为正整数", stream_id)
                            return True, "参数错误", 2

                        if start_chapter is not None and end_chapter is not None and start_chapter > end_chapter:
                            await self.ctx.send.text("起始章节不能大于结束章节", stream_id)
                            return True, "参数错误", 2

                        chapter_range = (start_chapter, end_chapter)
                except ValueError:
                    await self.ctx.send.text("章节格式错误，请使用：/jm ID 或 /jm ID 章节号 或 /jm ID 起始-结束", stream_id)
                    return True, "参数错误", 2
            else:
                # 单个章节号
                try:
                    chapter_num = int(chapter_param)
                    if chapter_num <= 0:
                        await self.ctx.send.text("章节数必须为正整数", stream_id)
                        return True, "参数错误", 2
                except ValueError:
                    await self.ctx.send.text("章节数必须为正整数", stream_id)
                    return True, "参数错误", 2

        # 权限检查（仅在下载时）
        owner_qq = self.config.jm.owner_qq
        if owner_qq:
            user_id = self._get_user_id(kwargs)
            if user_id != owner_qq:
                await self.ctx.send.text("不可以瑟瑟哦", stream_id)
                return True, "权限不足", 2

        await self.ctx.send.text("开始下载", stream_id)

        max_pdf_pages = self.config.jm.max_pdf_pages

        success, chapter_count, error_msg = await check_album_chapters(
            album_id,
            output_dir=self._jm_data_dir,
        )

        if not success:
            await self.ctx.send.text(f"检查章节信息失败: {error_msg}", stream_id)
            return True, error_msg or "检查章节信息失败", 2

        # 决定下载策略
        only_first_chapter = False
        use_chapter_index = None
        use_chapter_range = None

        if chapter_count > 1:
            if chapter_range is not None:
                # 处理章节范围
                start_chapter, end_chapter = chapter_range

                # 如果没有指定起始章节，默认从第1章开始
                if start_chapter is None:
                    start_chapter = 1

                # 如果没有指定结束章节，默认下载到最后一章
                if end_chapter is None:
                    end_chapter = chapter_count

                # 调整范围不超过实际章节数
                if start_chapter > chapter_count:
                    start_chapter = chapter_count
                    await self.ctx.send.text(f"本子仅有{chapter_count}章，调整起始章节为第{start_chapter}章", stream_id)

                if end_chapter > chapter_count:
                    end_chapter = chapter_count
                    await self.ctx.send.text(f"本子仅有{chapter_count}章，调整结束章节为第{end_chapter}章", stream_id)

                use_chapter_range = (start_chapter, end_chapter)
                if start_chapter == end_chapter:
                    await self.ctx.send.text(f"检测到多章节本子({chapter_count}话)，下载第{start_chapter}章", stream_id)
                else:
                    await self.ctx.send.text(f"检测到多章节本子({chapter_count}话)，下载第{start_chapter}到{end_chapter}章", stream_id)
            elif chapter_num is not None:
                if chapter_num > chapter_count:
                    only_first_chapter = True
                    await self.ctx.send.text(f"本子仅有{chapter_count}章，忽略无效的章节数，下载第一章", stream_id)
                else:
                    use_chapter_index = chapter_num
                    await self.ctx.send.text(f"检测到多章节本子({chapter_count}话)，下载第{chapter_num}章", stream_id)
            else:
                # 多章节本子但没有指定章节，提示用户
                await self.ctx.send.text(f"检测到多章节本子({chapter_count}话)，请指定要下载的章节", stream_id)
                await self.ctx.send.text(f"例如：/jm {album_id} 1 或 /jm {album_id} 1-5", stream_id)
                return True, "请指定章节", 2
        else:
            if chapter_num is not None:
                await self.ctx.send.text("单章节本子，忽略章节数参数", stream_id)
            elif chapter_range is not None:
                await self.ctx.send.text("单章节本子，忽略章节范围参数", stream_id)

        success, album_dir, total_chapters = await async_download_album(
            album_id,
            output_dir=self._jm_data_dir,
            only_first_chapter=only_first_chapter,
            chapter_index=use_chapter_index,
            chapter_range=use_chapter_range,
        )

        if not success:
            await self.ctx.send.text("下载失败", stream_id)
            return True, album_dir or "下载失败", 2

        album_name = os.path.basename(album_dir)
        await self.ctx.send.text(album_name, stream_id)
        await self.ctx.send.text("下载成功", stream_id)

        # 收集图片文件
        supported_extensions = {".jpg", ".jpeg", ".png", ".webp"}
        img_paths = sorted(
            [p for p in Path(album_dir).rglob("*") if p.suffix.lower() in supported_extensions],
            key=lambda p: p.as_posix()
        )

        if not img_paths:
            await self.ctx.send.text("未找到图片", stream_id)
            return True, "未找到图片", 2

        if len(img_paths) > max_pdf_pages:
            await self.ctx.send.text(f"页数超过 {max_pdf_pages}，不生成 PDF", stream_id)
            return True, "页数超限", 2

        # 检查是否使用加密压缩包
        use_encrypted_zip = self.config.jm.use_encrypted_zip
        fake_extension = self.config.jm.fake_extension

        if use_encrypted_zip:
            return await self._send_encrypted_zip(
                img_paths, album_id, use_chapter_index, use_chapter_range,
                only_first_chapter, fake_extension, stream_id, kwargs,
            )
        else:
            return await self._send_pdf(
                img_paths, album_id, use_chapter_index, use_chapter_range,
                only_first_chapter, stream_id, kwargs,
            )

    def _build_file_name(
        self, album_id: str, use_chapter_index: Optional[int],
        use_chapter_range: Optional[Tuple[int, int]], only_first_chapter: bool,
    ) -> str:
        """按下载策略生成安全的文件基名。"""
        safe_name = sanitize_filename(album_id)
        if use_chapter_range is not None:
            start_chapter, end_chapter = use_chapter_range
            if start_chapter == end_chapter:
                safe_name = f"{safe_name}_{start_chapter:02d}"
            else:
                safe_name = f"{safe_name}_{start_chapter:02d}-{end_chapter:02d}"
        elif use_chapter_index is not None:
            safe_name = f"{safe_name}_{use_chapter_index:02d}"
        elif only_first_chapter:
            safe_name = f"{safe_name}_01"
        return safe_name

    async def _send_encrypted_zip(
        self, img_paths: List[Path], album_id: str,
        use_chapter_index: Optional[int], use_chapter_range: Optional[Tuple[int, int]],
        only_first_chapter: bool, fake_extension: str, stream_id: str,
        kwargs: Dict[str, Any],
    ) -> Tuple[bool, Optional[str], int]:
        """生成 PDF、加密压缩包并发送（伪装格式）。"""
        safe_name = self._build_file_name(album_id, use_chapter_index, use_chapter_range, only_first_chapter)

        pdf_dir = os.path.join(self._plugin_dir, "tmp_pdf")
        os.makedirs(pdf_dir, exist_ok=True)
        pdf_path = os.path.join(pdf_dir, f"{safe_name}.pdf")

        zip_dir = os.path.join(self._plugin_dir, "tmp_zip")
        os.makedirs(zip_dir, exist_ok=True)
        zip_path = os.path.join(zip_dir, f"{safe_name}.zip")
        fake_zip_path = os.path.join(zip_dir, f"{safe_name}{fake_extension}")

        try:
            await asyncio.to_thread(images_to_pdf_sync, img_paths, pdf_path)

            await asyncio.to_thread(pdf_to_encrypted_zip, pdf_path, zip_path, safe_name)

            await self.ctx.send.text("压缩并加密成功", stream_id)

            shutil.move(zip_path, fake_zip_path)

            scope, target_id = self._get_napcat_target(kwargs)
            if not scope:
                await self.ctx.send.text("无法识别发送对象", stream_id)
                return True, None, 2

            ok, msg = await upload_pdf_via_napcat(
                fake_zip_path, f"{safe_name}{fake_extension}", scope, target_id,
                self.config.jm.napcat_base_url, self.config.jm.napcat_token,
                self.config.jm.enable_wsl_path,
            )

            if not ok:
                await self.ctx.send.text("上传失败", stream_id)
                return True, msg, 2

            try:
                os.remove(pdf_path)
                os.remove(fake_zip_path)
            except Exception:
                pass

            await self.ctx.send.text(f"完成！密码是文件名（不含扩展名）：{safe_name}", stream_id)
            return True, "完成", 2

        except Exception as e:
            await self.ctx.send.text(f"处理失败: {e}", stream_id)
            try:
                if os.path.exists(pdf_path):
                    os.remove(pdf_path)
                if os.path.exists(zip_path):
                    os.remove(zip_path)
                if os.path.exists(fake_zip_path):
                    os.remove(fake_zip_path)
            except Exception:
                pass
            return True, str(e), 2

    async def _send_pdf(
        self, img_paths: List[Path], album_id: str,
        use_chapter_index: Optional[int], use_chapter_range: Optional[Tuple[int, int]],
        only_first_chapter: bool, stream_id: str,
        kwargs: Dict[str, Any],
    ) -> Tuple[bool, Optional[str], int]:
        """生成并发送 PDF。"""
        safe_name = self._build_file_name(album_id, use_chapter_index, use_chapter_range, only_first_chapter)

        pdf_dir = os.path.join(self._plugin_dir, "tmp_pdf")
        os.makedirs(pdf_dir, exist_ok=True)
        pdf_path = os.path.join(pdf_dir, f"{safe_name}.pdf")

        try:
            await asyncio.to_thread(images_to_pdf_sync, img_paths, pdf_path)
        except Exception as e:
            await self.ctx.send.text("PDF 生成失败", stream_id)
            return True, str(e), 2

        # 确定上传目标
        scope, target_id = self._get_napcat_target(kwargs)
        if not scope:
            await self.ctx.send.text("无法识别发送对象", stream_id)
            return True, None, 2

        ok, msg = await upload_pdf_via_napcat(
            pdf_path, f"{safe_name}.pdf", scope, target_id,
            self.config.jm.napcat_base_url, self.config.jm.napcat_token,
            self.config.jm.enable_wsl_path,
        )

        if not ok:
            await self.ctx.send.text("上传失败", stream_id)
            return True, msg, 2

        try:
            os.remove(pdf_path)
        except Exception:
            pass

        return True, "完成", 2

    @Command(
        "jm搜图",
        description="以图搜本：发送图片后输入 /jm搜图，自动在 JM 中搜索。",
        pattern=r"^(?:@[^\s]+\s*)?/jm搜图$",
    )
    async def handle_jm_sotu(self, **kwargs: Any) -> Tuple[bool, Optional[str], int]:
        """JM 以图搜本命令处理。

        流程：获取图片（当前消息 → get_by_id 兜底 → 1 小时历史）→ SauceNAO 搜索
        → 提取最佳匹配标题 → JM 关键词搜索 → 显示前 5 条结果。
        """
        stream_id = kwargs["stream_id"]

        # 1. 获取图片
        image_data = None
        try:
            image_data = await self._acquire_image(kwargs)
        except Exception:
            pass

        if not image_data:
            await self.ctx.send.text("未检测到图片，请发送图片", stream_id)
            return True, "未检测到图片", 2

        # 2. 保存图片到临时目录
        cache_dir = os.path.join(self._plugin_dir, "tmp_sotu")
        os.makedirs(cache_dir, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        temp_image_path = os.path.join(cache_dir, f"upload_{timestamp}.png")

        try:
            if isinstance(image_data, str):
                with open(temp_image_path, 'wb') as f:
                    f.write(base64.b64decode(image_data))
            elif isinstance(image_data, bytes):
                with open(temp_image_path, 'wb') as f:
                    f.write(image_data)
            else:
                await self.ctx.send.text("图片格式无法识别", stream_id)
                return True, "图片格式无法识别", 2
        except Exception as e:
            await self.ctx.send.text(f"保存图片失败: {e}", stream_id)
            return True, f"保存图片失败: {e}", 2

        await self.ctx.send.text("正在进行以图搜图...", stream_id)

        # 3. 调用 SauceNAO
        success, result = await search_image_by_saucenao(
            temp_image_path,
            proxy=self.config.sotu.proxy,
            api_key=self.config.sotu.api_key,
        )
        try:
            os.remove(temp_image_path)
        except Exception:
            pass

        if not success or not result or not result.get('results'):
            error_msg = result if isinstance(result, str) else "未知错误"
            await self.ctx.send.text(f"搜图失败: {error_msg}", stream_id)
            return True, f"搜图失败: {error_msg}", 2

        # 4. 提取最佳结果标题
        # SauceNAO 结果结构:
        # { "results": [ { "header": {...}, "data": { "title": "...", "jp_name": "...", "eng_name": "..." } } ] }
        best_match = result['results'][0]
        data = best_match.get('data', {})
        title = data.get('title') or data.get('jp_name') or data.get('eng_name')

        if not title:
            # 尝试从 source 或其他字段获取
            title = data.get('source') or data.get('pixiv_id')
            if title and str(title).isdigit():
                title = f"Pixiv ID {title}"

        if not title:
            await self.ctx.send.text("搜图成功但无法获取标题", stream_id)
            return True, "无法获取标题", 2

        sim = best_match.get('header', {}).get('similarity', '0')
        await self.ctx.send.text(f"搜图最佳匹配: [{sim}%] {title}\n正在 JM 中搜索...", stream_id)

        # 5. 在 JM 中搜索
        jm_data_dir = self._jm_data_dir
        os.makedirs(jm_data_dir, exist_ok=True)

        # 初始化 jmcomic option
        option = None
        try:
            import jmcomic
            option_file = os.path.join(jm_data_dir, "option.yml")

            # 读取配置中的代理（jm 无独立代理配置，沿用 sotu.proxy）
            proxy = self.config.sotu.proxy

            with open(option_file, "w", encoding="utf-8") as config_file:
                config_content = f"dir_rule:\n  base_dir: {jm_data_dir}\n"

                # 如果有代理，写入 client 配置
                if proxy:
                    config_content += f"""
client:
  postman:
    meta_data:
      proxies:
        http: {proxy}
        https: {proxy}
"""
                config_file.write(config_content)

            option = jmcomic.create_option_by_file(option_file)

        except Exception as e:
            await self.ctx.send.text(f"JM 配置初始化失败: {e}", stream_id)
            return True, f"JM 配置初始化失败: {e}", 2

        if not option:
            await self.ctx.send.text("JM 配置初始化失败: option 未定义", stream_id)
            return True, "JM 配置初始化失败", 2

        jm_results = await search_jm_by_keyword(title, option)

        # 优化：如果第一次精确搜索没结果，尝试清洗标题后再搜
        # 如果第一次有结果，但结果很少（比如少于3个），也可以尝试清洗后再搜并合并结果

        if not jm_results:
            # 尝试简化标题（去除括号等）再搜一次
            # 策略：
            # 1. 移除 (Cxxx) (xxx) [xxx] 等标记，只保留核心词
            # 2. 如果还是搜不到，可能需要提取 "神力に揺蕩う" 这样的核心短语

            clean_title = re.sub(r'[\(\[\{（【].*?[\)\]\}）】]', '', title).strip()
            # 修复：移除可能残留的闭合括号 ] 或 )
            clean_title = clean_title.replace(']', '').replace(')', '').replace('】', '').replace('）', '').strip()

            if clean_title and clean_title != title:
                await self.ctx.send.text(f"精确搜索无结果，尝试搜索核心词: {clean_title}", stream_id)
                jm_results = await search_jm_by_keyword(clean_title, option)

        if not jm_results:
            await self.ctx.send.text(f"JM 中未找到相关本子: {title}", stream_id)
            return True, f"JM 中未找到相关本子: {title}", 2

        # 6. 显示结果
        msg = f"JM 搜索结果 ({len(jm_results)}个):\n"
        for i, album in enumerate(jm_results[:5], 1):
            msg += f"{i}. ID: {album['id']} | {album['title']} | {album.get('author', '未知')}\n"

        # 增加提示信息
        msg += "\n请输入 /jm ID 进行下载"
        if len(jm_results) > 5:
            msg += f"\n(仅显示前5个，共找到 {len(jm_results)} 个)"

        await self.ctx.send.text(msg, stream_id)
        return True, "搜图结果已发送", 2


def create_plugin():
    """插件工厂函数。"""
    return JMPlugin()
