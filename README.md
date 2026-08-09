# JM 插件 (JMComic Plugin for MaiBot)

> 基于 `jmcomic` 的 MaiBot 插件，支持专辑/章节下载、PDF 合成、加密压缩、以图搜本与 Napcat 上传

## 📖 简介

JM 搜图插件是为 [MaiBot](https://github.com/MaiM-with-u/MaiBot) 开发的内容下载与分享插件，是由 [https://github.com/yumemi1/jm_plugin](https://github.com/yumemi1/jm_plugin) 二次开发而来，添加了以图搜本和关键词搜索的功能，并且增加了加密压缩包，面向 JMComic 图源。它可以：
- 解析并下载指定本子（支持多章节选择）
- 将图片合并为 PDF（可限制最大页数）
- 生成加密压缩包并伪装格式（可选）
- 通过 Napcat 接口上传到 QQ 群或私聊
- 支持以图搜本和关键词搜索
- 支持主人权限控制

## ✨ 功能特性

- 📥 **本子下载**：输入本子 ID 即可下载（多章节必须指定章节）
- 🧩 **章节选择**：多章节专辑可指定下载某一章或章节范围
- 📄 **PDF 合成**：自动将图片集合转换为 PDF 文件
- 🔒 **加密压缩**：可将 PDF 加密压缩并伪装格式（密码为文件名）
- 🔍 **以图搜本**：发送图片后使用 `/jm搜图` 搜索本子
- 🔎 **关键词搜索**：使用 `/jm 关键词` 搜索本子
- 👤 **权限控制**：可设置主人 QQ 号限制下载权限
- 🚚 **Napcat 上传**：支持群聊/私聊文件上传
- 🔢 **页数限制**：可配置 `max_pdf_pages` 限制 PDF 页数
- 🧹 **自动清理**：完成后自动清理临时文件

## 📦 安装

将本插件文件夹放入 MaiBot 的 `plugins/` 目录，并命名为 `jm_soutu`。

**使用 Git 安装**（将 `<MAIBOT_DIR>` 替换为你的 MaiBot 安装路径）：

```cmd
cd <MAIBOT_DIR>\plugins
git clone https://github.com/voktoylo/jm_soutu.git
```

**手动复制**：将本仓库文件夹复制到 `<MAIBOT_DIR>\plugins\jm_soutu`。

### 依赖说明

- 插件依赖通过 `_manifest.json` 的 `dependencies` 字段声明（jmcomic、aiohttp、curl_cffi、olefile、Pillow、pyzipper），由宿主统一管理。
- 如需手动安装，请确保以下库可用：

```cmd
pip install jmcomic pillow aiohttp pyzipper olefile curl_cffi
```

## ⚙️ 配置

在 `config.toml` 中的 `plugin`、`jm` 和 `sotu` 段进行配置：

```toml
[plugin]
config_version = "1.0.0"
enabled = true

[jm]
jm_data_dir = ""                       # 下载目录（留空则使用 data/plugins/yumemi1.jm_plugin/）
napcat_base_url = "http://127.0.0.1:3000"  # Napcat 基础 URL
max_pdf_pages = 300                     # PDF 最大页数
owner_qq = ""                          # 主人 QQ 号（留空则不限制下载）
use_encrypted_zip = true                # 是否使用加密压缩包（密码为文件名）
fake_extension = ".dat"                 # 压缩包伪装格式

[sotu]
api_key = ""                            # SauceNAO API Key
proxy = ""                             # 搜图代理（可选）
```

### 参数说明

**[plugin] 配置项：**
- `config_version`：配置文件版本号（与插件配置模型保持一致，勿随意修改）
- `enabled`：是否启用插件

**[jm] 配置项：**
- `jm_data_dir`：图集下载存储位置（相对/绝对路径均可；留空则使用 `data/plugins/yumemi1.jm_plugin/`）
- `napcat_base_url`：Napcat 上传 API 基础地址（默认 http://127.0.0.1:3000）
  - Napcat 需要启用 HTTP 服务器，端口可在 Napcat 配置中修改
- `max_pdf_pages`：限制每个 PDF 的最大页数，防止过大文件
- `owner_qq`：主人 QQ 号，只有此 QQ 号才能下载（留空则不限制）
- `use_encrypted_zip`：是否将 PDF 加密压缩并伪装格式（默认 false）
- `fake_extension`：压缩包伪装的文件扩展名（如 `.dat`, `.bin`, `.tmp` 等）

**[sotu] 配置项：**
- `api_key`：SauceNAO API Key（必填，否则会有搜索限制）
- `proxy`：搜图使用的代理地址（可选；同时作为 JM 搜索代理使用）

## 🎮 使用方法

### 命令格式

```
/jm <本子ID>                    # 下载指定本子（多章节必须指定章节）
/jm <本子ID> <章节序号>         # 下载指定章节
/jm <本子ID> <起始章节>-<结束章节>  # 下载指定范围的章节（如 1-5）
/jm <本子ID> -<结束章节>        # 从第 1 章下载到指定章节（如 -5）
/jm <本子ID> <起始章节>-        # 从指定章节下载到最后一章（如 3-）
/jm <关键词>                    # 搜索本子
/jm <关键词> <页码>             # 搜索本子（指定页码）
/jm搜图                         # 以图搜本
```

命令前可带 `@机器人` 前缀，如 `@机器人 /jm 123`。

### 下载功能

**行为规则：**
- 多章节本子：**必须指定章节**，否则会提示用户选择章节；可使用 `<章节序号>` 参数下载指定章节；可使用 `<起始章节>-<结束章节>` 参数下载章节范围
- 单章节本子：忽略 `<章节序号>` 和章节范围参数，按单章处理
- 权限控制：如果设置了 `owner_qq`，只有主人才能下载，其他人会收到 "不可以瑟瑟哦" 提示

**章节范围语法：**
- `1-5`：下载第 1 到 5 章
- `-5`：从第 1 章下载到第 5 章
- `3-`：从第 3 章下载到最后一章
- 超出实际章节数时，会自动调整到有效范围

**文件格式：**
- 默认模式：生成 PDF 文件
- 加密压缩模式：生成加密 ZIP 压缩包并伪装格式（密码为文件名）

**文件命名：**
- 未指定章节或默认全部：`JM_<ID>.pdf` 或 `JM_<ID>.dat`
- 指定章节 N：`JM_<ID>_<NN>.pdf` 或 `JM_<ID>_<NN>.dat`（两位序号）
- 指定章节范围 N-M：`JM_<ID>_<NN>-<MM>.pdf` 或 `JM_<ID>_<NN>-<MM>.dat`（两位序号范围）

**示例：**
```
/jm 123456           # 下载本子 123456（多章节必须指定章节）
/jm 123456 2         # 下载本子 123456 的第 2 章
/jm 123456 1-5       # 下载本子 123456 的第 1 到 5 章
/jm 123456 -5        # 下载本子 123456 的第 1 到 5 章
/jm 123456 3-        # 下载本子 123456 的第 3 章到最后一章
/jm 123456 99        # 超出范围时，下载第 1 章并提示
```

### 搜索功能

**关键词搜索：**
```
/jm 关键词            # 搜索包含关键词的本子
/jm 关键词 2          # 搜索并显示第 2 页结果
```

**以图搜本：**
```
/jm搜图              # 搜索最近发送的图片对应的本子
```

### 加密压缩包使用

如果启用了 `use_encrypted_zip = true`，下载的文件会：
1. 自动加密压缩（密码为文件名，不含扩展名）
2. 伪装成指定格式（如 `.dat`）
3. 用户收到后需要：
   - 将文件扩展名改回 `.zip`
   - 使用文件名作为密码解压
   - 解压后获得 PDF 文件

**示例：**
```
收到文件：JM_123456_01.dat
步骤：
1. 改名为：JM_123456_01.zip
2. 解压密码：JM_123456_01
3. 获得：JM_123456_01.pdf
```

## ⚠️ 注意事项

- 请确保 `napcat_base_url` 正确且 Napcat 服务已启动
  - Napcat 需要启用 HTTP 服务器，默认端口 3000，可在 Napcat 配置中修改
- 生成 PDF 时可能占用较多内存与磁盘空间，建议设置合理的 `max_pdf_pages`
- 网络环境可能影响下载速度；`jmcomic` 在某些网络条件下需要额外配置（代理/Cookie）
- 使用加密压缩包时，文件体积会减小约 30-50%，但用户需要手动解压
- 搜图功能需要配置 SauceNAO API Key，否则会有搜索限制
- 权限控制功能仅限制下载，搜索和搜图功能不受影响
- **旧版数据**：v2.0.0 起下载数据默认存储于 `data/plugins/yumemi1.jm_plugin/`，插件目录下旧 `data/` 中的历史下载数据不会自动迁移，如需保留请手动迁移或重新下载
- 请遵守相关法律法规与平台规范，不要传播非法或不当内容

## 🙏 鸣谢

- [JMComic-Crawler-Python](https://github.com/hect0x7/JMComic-Crawler-Python) - 图源与下载框架
- [MaiBot](https://github.com/MaiM-with-u/MaiBot) - 优秀的 QQ 机器人宿主框架
- [Napcat](https://github.com/NapNeko/NapcatQQ) - QQ 上传接口支持

## 📄 许可证

本插件采用 MIT 许可证开源，详见 `LICENSE` 文件。
