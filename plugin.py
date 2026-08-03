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
import asyncio
import os
import re
import json
import base64
import shutil
import zipfile
from datetime import datetime
from typing import List, Tuple, Type, Optional, Dict, Any
from pathlib import Path

import aiohttp
try:
    from curl_cffi.requests import AsyncSession
    HAS_CURL_CFFI = True
except ImportError:
    HAS_CURL_CFFI = False

try:
    import pyzipper
    HAS_PYZIPPER = True
except ImportError:
    HAS_PYZIPPER = False

from PIL import Image
from src.plugin_system.apis import message_api

from src.plugin_system import (
    BasePlugin,
    register_plugin,
    BaseCommand,
    ComponentInfo,
    ConfigField,
)
from src.config.config import global_config



# =============================================================================
# 工具函数
# =============================================================================

def sanitize_filename(name: str) -> str:
    """将文件名中的非法字符替换为下划线。

    替换字符包括：\ / : * ? " < > | 以及换行符。

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
        raise Exception(f"压缩失败: {e}")



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

def _find_image_in_segments(message_segments) -> Optional[str]:
    """从消息段中递归提取图片数据"""
    if not message_segments:
        return None
    
    # 如果是列表
    if isinstance(message_segments, list):
        for seg in message_segments:
            res = _find_image_in_segments(seg)
            if res: return res
        return None
        
    # 如果是对象（检查属性）
    if hasattr(message_segments, 'type') and hasattr(message_segments, 'data'):
        if message_segments.type in ('image', 'emoji'):
            return message_segments.data
        if message_segments.type == 'seglist':
            return _find_image_in_segments(message_segments.data)
            
    return None

async def _find_image_from_history(chat_id: str) -> Optional[str]:
    """从历史消息中查找最近的图片"""
    if not chat_id:
        return None
    try:
        # 获取最近 15 条消息
        recent_messages = message_api.get_recent_messages(chat_id, hours=1.0, limit=15, filter_mai=True)
        for msg in reversed(recent_messages):
            # 检查 is_picid 属性
            is_picid = False
            if isinstance(msg, dict):
                is_picid = msg.get('is_picid', False)
            else:
                is_picid = getattr(msg, 'is_picid', False)
            
            if is_picid:
                # 尝试从消息段提取
                message_segment = None
                if isinstance(msg, dict):
                    message_segment = msg.get('message_segment')
                else:
                    message_segment = getattr(msg, 'message_segment', None)
                
                if message_segment:
                    img_data = _find_image_in_segments(message_segment)
                    if img_data:
                        return img_data
    except Exception:
        pass
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
        import jmcomic
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
                # 增加错误日志打印，方便调试
                print(f"JM Search Error: {e}")
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
        print(f"Search Wrapper Error: {e}")
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
    plugin_dir: Optional[str] = None,
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
        plugin_dir: 插件目录，用于确定默认的 data 目录位置
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
        if plugin_dir:
            output_dir = os.path.join(plugin_dir, "data")
        else:
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
# 命令处理
# =============================================================================

class JMSotuCommand(BaseCommand):
    """JM 以图搜本命令处理器"""
    command_name = "jm搜图"
    command_description = "发送图片进行搜图，并自动在 JM 中搜索结果。用法：发送图片后输入 /jm搜图"
    command_pattern = r"(?:.*，说：\s*)?/jm搜图\b"
    intercept_message = True

    async def execute(self) -> Tuple[bool, Optional[str], bool]:
        if not aiohttp:
            await self.send_text("依赖缺失: aiohttp")
            return True, None, True

        # 1. 获取图片
        image_data = None
        try:
            # Message Segment
            if hasattr(self.message, 'message_segment') and self.message.message_segment:
                image_data = _find_image_in_segments(self.message.message_segment)
            # Image Data
            if not image_data and hasattr(self.message, 'image_data') and self.message.image_data:
                image_data = self.message.image_data
            # Content
            if not image_data and hasattr(self.message, 'content'):
                content = self.message.content
                if isinstance(content, dict) and 'image' in content:
                    image_data = content['image']
            # History
            if not image_data:
                chat_id = None
                try:
                    chat_stream = getattr(self.message, 'chat_stream', None)
                    if chat_stream and getattr(chat_stream, 'stream_id', None):
                        chat_id = chat_stream.stream_id
                except Exception: pass
                if chat_id:
                    image_data = await _find_image_from_history(chat_id)
        except Exception:
            pass

        if not image_data:
            await self.send_text("未检测到图片，请发送图片")
            return True, None, True

        # 2. 保存图片到临时目录
        plugin_dir = os.path.dirname(os.path.abspath(__file__))
        cache_dir = os.path.join(plugin_dir, "tmp_sotu")
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
                await self.send_text("图片格式无法识别")
                return True, None, True
        except Exception as e:
            await self.send_text(f"保存图片失败: {e}")
            return True, None, True

        await self.send_text("正在进行以图搜图...")
        
        # 3. 调用 SauceNAO
        # 尝试从 config 读取代理配置
        sotu_proxy = self.get_config("sotu.proxy", None) 
        saucenao_api_key = self.get_config("sotu.api_key", "1115d151d73f404c1522bdf28542a50b42c27192")
        
        success, result = await search_image_by_saucenao(temp_image_path, proxy=sotu_proxy, api_key=saucenao_api_key)
        try: os.remove(temp_image_path)
        except: pass

        if not success or not result or not result.get('results'):
            error_msg = result if isinstance(result, str) else "未知错误"
            await self.send_text(f"搜图失败: {error_msg}")
            return True, None, True

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
            await self.send_text("搜图成功但无法获取标题")
            return True, None, True

        sim = best_match.get('header', {}).get('similarity', '0')
        await self.send_text(f"搜图最佳匹配: [{sim}%] {title}\n正在 JM 中搜索...")

        # 5. 在 JM 中搜索
        jm_data_dir = self.get_config("jm.jm_data_dir","")
        if not jm_data_dir:
            jm_data_dir = os.path.join(plugin_dir, "data")
        os.makedirs(jm_data_dir, exist_ok=True)
        
        # 初始化 jmcomic option
        option = None
        try:
            import jmcomic
            option_file = os.path.join(jm_data_dir, "option.yml")
            
            # 读取配置中的代理
            proxy = self.get_config("jm.proxy", "")
            if not proxy:
                # 尝试读取 sotu 的代理作为回退
                proxy = self.get_config("sotu.proxy", "")
            
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
            
            # 移除强制配置，完全使用 jmcomic 默认行为（自动更新域名）
            
        except Exception as e:
            await self.send_text(f"JM 配置初始化失败: {e}")
            return True, None, True

        if not option:
            await self.send_text("JM 配置初始化失败: option 未定义")
            return True, None, True

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
                await self.send_text(f"精确搜索无结果，尝试搜索核心词: {clean_title}")
                jm_results = await search_jm_by_keyword(clean_title, option)
        
        if not jm_results:
            await self.send_text(f"JM 中未找到相关本子: {title}")
            return True, None, True

        # 6. 显示结果
        msg = f"JM 搜索结果 ({len(jm_results)}个):\n"
        for i, album in enumerate(jm_results[:5], 1):
            msg += f"{i}. ID: {album['id']} | {album['title']} | {album.get('author', '未知')}\n"
        
        # 增加提示信息
        msg += "\n请输入 /jm ID 进行下载"
        if len(jm_results) > 5:
             msg += f"\n(仅显示前5个，共找到 {len(jm_results)} 个)"
        
        await self.send_text(msg)
        return True, None, True


class JMCommand(BaseCommand):
    """JM 本子下载命令处理器。

    命令格式：
    - /jm <ID>        下载指定 ID 的本子（多章节默认第 1 章）
    - /jm <ID> <章节> 下载指定 ID 本子的指定章节

    执行流程：解析参数 → 检查章节 → 决策下载 → 合成 PDF → 上传 → 清理。
    """
    
    command_name = "jm"
    command_description = "下载或搜索 JM 本子。用法：/jm ID（多章节必须指定章节）或 /jm ID 章节号 或 /jm ID 起始-结束 或 /jm 关键词"
    command_pattern = r"^/jm(?:\s+(?P<args>.+))?$"

    async def execute(self) -> Tuple[bool, Optional[str], bool]:
        """执行命令主逻辑。
        
        功能：
        1. /jm 关键词 -> 搜索本子并返回列表
        2. /jm ID -> 下载指定本子
        3. /jm ID 章节数 -> 下载指定章节
        
        Returns:
            元组 (是否继续执行, 执行结果消息, 是否成功)
        """
        args_str = ""
        if self.matched_groups and "args" in self.matched_groups:
            args_str = self.matched_groups["args"] or ""
        args_str = args_str.strip()
        
        if not args_str:
            await self.send_text("用法：/jm ID 或 /jm ID 章节数")
            return True, None, True
        
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
                except:
                    pass
            elif len(parts) > 1:
                 # 关键词本身包含空格
                 album_id_or_keyword = args_str

            await self.send_text(f"正在搜索: {album_id_or_keyword} (第{search_page}页)")
            
            plugin_dir = os.path.dirname(os.path.abspath(__file__))
            jm_data_dir = self.get_config("jm.jm_data_dir","")
            if not jm_data_dir:
                jm_data_dir = os.path.join(plugin_dir, "data")
            os.makedirs(jm_data_dir, exist_ok=True)
            
            try:
                import jmcomic
                option_file = os.path.join(jm_data_dir, "option.yml")
                
                # 读取配置中的代理
                proxy = self.get_config("jm.proxy", "")
                if not proxy:
                    proxy = self.get_config("sotu.proxy", "")
                
                with open(option_file, "w", encoding="utf-8") as config_file:
                    config_content = f"dir_rule:\n  base_dir: {jm_data_dir}\n"
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
                
                # 移除强制配置，完全使用 jmcomic 默认行为（自动更新域名）
                
                jm_results = await search_jm_by_keyword(album_id_or_keyword, option, page=search_page)
                
                if not jm_results:
                    await self.send_text("未找到相关本子")
                    return True, None, True
                
                display_count = 10
                msg = f"搜索结果 (第{search_page}页, 显示{min(len(jm_results), display_count)}个):\n"
                for i, album in enumerate(jm_results[:display_count], 1):
                    msg += f"{i}. ID: {album['id']} | {album['title']}\n"
                
                msg += "\n请输入 /jm ID 进行下载"
                msg += f"\n请输入 /jm {album_id_or_keyword} {search_page + 1} 查看下一页"
                
                await self.send_text(msg)
                return True, None, True
                
            except Exception as e:
                await self.send_text(f"搜索失败: {e}")
                return True, None, True
                
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
                            await self.send_text("起始章节必须为正整数")
                            return True, None, True
                        
                        if end_chapter is not None and end_chapter <= 0:
                            await self.send_text("结束章节必须为正整数")
                            return True, None, True
                        
                        if start_chapter is not None and end_chapter is not None and start_chapter > end_chapter:
                            await self.send_text("起始章节不能大于结束章节")
                            return True, None, True
                        
                        chapter_range = (start_chapter, end_chapter)
                except ValueError:
                    await self.send_text("章节格式错误，请使用：/jm ID 或 /jm ID 章节号 或 /jm ID 起始-结束")
                    return True, None, True
            else:
                # 单个章节号
                try:
                    chapter_num = int(chapter_param)
                    if chapter_num <= 0:
                        await self.send_text("章节数必须为正整数")
                        return True, None, True
                except ValueError:
                    await self.send_text("章节数必须为正整数")
                    return True, None, True

        # 权限检查（仅在下载时）
        owner_qq = self.get_config("jm.owner_qq", "")
        if owner_qq:
            try:
                message_info = self.message.message_info
                user_id = None
                if message_info.user_info and message_info.user_info.user_id:
                    user_id = str(message_info.user_info.user_id)
                
                if user_id != owner_qq:
                    await self.send_text("不可以瑟瑟哦")
                    return True, None, False
            except Exception:
                pass

        await self.send_text("开始下载")

        plugin_dir = os.path.dirname(os.path.abspath(__file__))
        jm_data_dir = self.get_config("jm.jm_data_dir","")
        napcat_base_url = self.get_config("jm.napcat_base_url")
        napcat_token = self.get_config("jm.napcat_token", "")
        enable_wsl_path = self.get_config("jm.enable_wsl_path", False)
        max_pdf_pages = self.get_config("jm.max_pdf_pages")

        success, chapter_count, error_msg = await check_album_chapters(
            album_id,
            output_dir=jm_data_dir or None,
        )

        if not success:
            await self.send_text(f"检查章节信息失败: {error_msg}")
            return True, error_msg, True

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
                    await self.send_text(f"本子仅有{chapter_count}章，调整起始章节为第{start_chapter}章")
                
                if end_chapter > chapter_count:
                    end_chapter = chapter_count
                    await self.send_text(f"本子仅有{chapter_count}章，调整结束章节为第{end_chapter}章")
                
                use_chapter_range = (start_chapter, end_chapter)
                if start_chapter == end_chapter:
                    await self.send_text(f"检测到多章节本子({chapter_count}话)，下载第{start_chapter}章")
                else:
                    await self.send_text(f"检测到多章节本子({chapter_count}话)，下载第{start_chapter}到{end_chapter}章")
            elif chapter_num is not None:
                if chapter_num > chapter_count:
                    only_first_chapter = True
                    await self.send_text(f"本子仅有{chapter_count}章，忽略无效的章节数，下载第一章")
                else:
                    use_chapter_index = chapter_num
                    await self.send_text(f"检测到多章节本子({chapter_count}话)，下载第{chapter_num}章")
            else:
                # 多章节本子但没有指定章节，提示用户
                await self.send_text(f"检测到多章节本子({chapter_count}话)，请指定要下载的章节")
                await self.send_text(f"例如：/jm {album_id} 1 或 /jm {album_id} 1-5")
                return True, None, True
        else:
            if chapter_num is not None:
                await self.send_text("单章节本子，忽略章节数参数")
            elif chapter_range is not None:
                await self.send_text("单章节本子，忽略章节范围参数")

        success, album_dir, total_chapters = await async_download_album(
            album_id,
            output_dir=jm_data_dir or None,
            plugin_dir=plugin_dir,
            only_first_chapter=only_first_chapter,
            chapter_index=use_chapter_index,
            chapter_range=use_chapter_range,
        )

        if not success:
            await self.send_text("下载失败")
            return True, album_dir, True

        album_name = os.path.basename(album_dir)
        await self.send_text(album_name)
        await self.send_text("下载成功")

    # 收集图片文件
        supported_extensions = {".jpg", ".jpeg", ".png", ".webp"}
        img_paths = sorted(
            [p for p in Path(album_dir).rglob("*") if p.suffix.lower() in supported_extensions],
            key=lambda p: p.as_posix()
        )

        if not img_paths:
            await self.send_text("未找到图片")
            return True, None, True

        if len(img_paths) > max_pdf_pages:
            await self.send_text(f"页数超过 {max_pdf_pages}，不生成 PDF")
            return True, None, True

    # 检查是否使用加密压缩包
        use_encrypted_zip = self.get_config("jm.use_encrypted_zip", False)
        fake_extension = self.get_config("jm.fake_extension", ".dat")

        if use_encrypted_zip:
            return await self._send_encrypted_zip(img_paths, album_id, use_chapter_index, use_chapter_range, only_first_chapter, plugin_dir, napcat_base_url, napcat_token, enable_wsl_path, fake_extension)
        else:
            return await self._send_pdf(img_paths, album_id, use_chapter_index, use_chapter_range, only_first_chapter, plugin_dir, napcat_base_url, napcat_token, enable_wsl_path)

    async def _send_encrypted_zip(self, img_paths, album_id, use_chapter_index, use_chapter_range, only_first_chapter, plugin_dir, napcat_base_url, napcat_token, enable_wsl_path, fake_extension):
        """生成PDF、加密压缩包并发送（伪装格式）"""
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

        pdf_dir = os.path.join(plugin_dir, "tmp_pdf")
        os.makedirs(pdf_dir, exist_ok=True)
        pdf_path = os.path.join(pdf_dir, f"{safe_name}.pdf")

        zip_dir = os.path.join(plugin_dir, "tmp_zip")
        os.makedirs(zip_dir, exist_ok=True)
        zip_path = os.path.join(zip_dir, f"{safe_name}.zip")
        fake_zip_path = os.path.join(zip_dir, f"{safe_name}{fake_extension}")

        try:
            await asyncio.to_thread(images_to_pdf_sync, img_paths, pdf_path)

            await asyncio.to_thread(pdf_to_encrypted_zip, pdf_path, zip_path, safe_name)

            await self.send_text("压缩并加密成功")

            shutil.move(zip_path, fake_zip_path)

            is_group = False
            group_id = None
            user_id = None
            
            try:
                message_info = self.message.message_info
                if message_info.group_info and message_info.group_info.group_id:
                    is_group = True
                    group_id = int(message_info.group_info.group_id)
                elif message_info.user_info and message_info.user_info.user_id:
                    user_id = int(message_info.user_info.user_id)
            except Exception:
                pass

            if is_group:
                ok, msg = await upload_pdf_via_napcat(
                    fake_zip_path, f"{safe_name}{fake_extension}", "group", group_id, napcat_base_url, napcat_token, enable_wsl_path
                )
            elif user_id:
                ok, msg = await upload_pdf_via_napcat(
                    fake_zip_path, f"{safe_name}{fake_extension}", "private", user_id, napcat_base_url, napcat_token, enable_wsl_path
                )
            else:
                await self.send_text("无法识别发送对象")
                return True, None, True

            if not ok:
                await self.send_text("上传失败")
                return True, msg, True

            try:
                os.remove(pdf_path)
                os.remove(fake_zip_path)
            except Exception:
                pass

            await self.send_text(f"完成！密码是文件名（不含扩展名）：{safe_name}")
            return True, "完成", True

        except Exception as e:
            await self.send_text(f"处理失败: {e}")
            try:
                if os.path.exists(pdf_path):
                    os.remove(pdf_path)
                if os.path.exists(zip_path):
                    os.remove(zip_path)
                if os.path.exists(fake_zip_path):
                    os.remove(fake_zip_path)
            except Exception:
                pass
            return True, str(e), True

    async def _send_pdf(self, img_paths, album_id, use_chapter_index, use_chapter_range, only_first_chapter, plugin_dir, napcat_base_url, napcat_token, enable_wsl_path):
        """生成并发送PDF"""
    # 生成 PDF
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
        pdf_dir = os.path.join(plugin_dir, "tmp_pdf")
        os.makedirs(pdf_dir, exist_ok=True)
        pdf_path = os.path.join(pdf_dir, f"{safe_name}.pdf")

        try:
            await asyncio.to_thread(images_to_pdf_sync, img_paths, pdf_path)
        except Exception as e:
            await self.send_text("PDF 生成失败")
            return True, str(e), True

    # 确定上传目标
        is_group = False
        group_id = None
        user_id = None
        
        try:
            message_info = self.message.message_info
            if message_info.group_info and message_info.group_info.group_id:
                is_group = True
                group_id = int(message_info.group_info.group_id)
            elif message_info.user_info and message_info.user_info.user_id:
                user_id = int(message_info.user_info.user_id)
        except Exception:
            pass

        if is_group:
            ok, msg = await upload_pdf_via_napcat(
                pdf_path, f"{safe_name}.pdf", "group", group_id, napcat_base_url, napcat_token, enable_wsl_path
            )
        elif user_id:
            ok, msg = await upload_pdf_via_napcat(
                pdf_path, f"{safe_name}.pdf", "private", user_id, napcat_base_url, napcat_token, enable_wsl_path
            )
        else:
            await self.send_text("无法识别发送对象")
            return True, None, True

        if not ok:
            await self.send_text("上传失败")
            return True, msg, True

        try:
            os.remove(pdf_path)
        except Exception:
            pass

        return True, "完成", True



# =============================================================================
# 插件注册
# =============================================================================

@register_plugin
class JMPlugin(BasePlugin):
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
    
    plugin_name: str = "jm_plugin"
    enable_plugin: bool = True

    dependencies: List[str] = []
    
    def __init__(self, plugin_dir: str):
        super().__init__(plugin_dir)

    @property
    def version(self) -> str:
        return "0.1.2"
    python_dependencies: List[str] = ["jmcomic", "pillow", "aiohttp", "curl_cffi", "pyzipper"]

    config_file_name: str = "config.toml"
    
    config_schema = {
        "jm": {
            "jm_data_dir": ConfigField(
                type=str,
                default="",
                description="JM 漫画下载目录，为空默认 plugins/jm_plugin/data"
            ),
            "napcat_base_url": ConfigField(
                type=str,
                default="http://127.0.0.1:3000",
                description="Napcat 机器人框架的 API 基础 URL"
            ),
            "napcat_token": ConfigField(
                type=str,
                default="",
                description="Napcat API 鉴权 Token（留空则不发送）"
            ),
            "enable_wsl_path": ConfigField(
                type=bool,
                default=False,
                description="是否将文件路径转换为 WSL 路径（Napcat 运行在 WSL 中时需启用）"
            ),
            "max_pdf_pages": ConfigField(
                type=int,
                default=300,
                description="PDF 最大页数限制"
            ),
            "owner_qq": ConfigField(
                type=str,
                default="",
                description="主人QQ号，只有此QQ号才能使用下载命令（为空则不限制）"
            ),
            "use_encrypted_zip": ConfigField(
                type=bool,
                default=False,
                description="是否生成加密压缩包（密码为文件名）并伪装格式"
            ),
            "fake_extension": ConfigField(
                type=str,
                default=".dat",
                description="压缩包伪装的文件扩展名（如 .dat, .bin, .tmp 等）"
            ),
        },
        "sotu": {
            "proxy": ConfigField(
                type=str,
                default="",
                description="搜图使用的代理地址 (http://... 或 socks5://...)，为空则直连"
            ),
            "api_key": ConfigField(
                type=str,
                default="1115d151d73f404c1522bdf28542a50b42c27192",
                description="SauceNAO API Key (必填，否则会有搜索限制)"
            )
        }
    }

    def get_plugin_components(self) -> List[Tuple[ComponentInfo, Type]]:
        """返回插件提供的命令组件列表。"""
        return [
            (JMCommand.get_command_info(), JMCommand),
            (JMSotuCommand.get_command_info(), JMSotuCommand),
        ]
