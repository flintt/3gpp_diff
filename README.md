# 3GPP Delta

面向 3GPP 技术规范的本地化版本对比工具。它从 3GPP 官方归档下载 Word 文稿，解析章节、正文、表格和技术图，并以章节树、Split Diff 或 Inline Diff 的方式展示不同 Release 之间的变化。

在线实例：<https://hq5001.mingdan.uk/>

> 本项目不是 3GPP 官方产品。规范文稿及其内容版权归相应权利人所有。

## 主要功能

- 比较任意两个已缓存版本，例如 Rel-18 → Rel-19、Rel-18 → Rel-20。
- 按章节显示 Added、Removed、Modified 和 Unchanged 状态。
- 支持 Split 与 Inline 两种正文 diff，并通过 tooltip 标明内容所属版本。
- 识别章节重编号、跨父章节移动以及父章节正文下沉，例如 `5 → 5.1`。
- 支持按章节标题或正文搜索，并在筛选状态下切换比较版本。
- 解析 Word 表格，包括多列表格、一列表格和简单横向合并；表格单元格也可显示 diff。
- 提取文稿内嵌图片，保留原始 WMF/EMF，并生成浏览器可显示的 PNG 预览。
- 支持亮色/暗色主题、响应式目录和变更章节快速导航。
- 将解析结果和版本组合 diff 缓存在磁盘，避免每次比较重复计算。

## 工作流程

1. 输入规范号，例如 `23.501` 或 `29.222`。
2. 服务从 3GPP 官方归档列出并下载每个 Release 的 `.0.0` 基线版本。
3. DOC 文稿经 LibreOffice 转为 DOCX，随后解析章节、表格和图片。
4. 后台优先预计算相邻 Release，再计算最近六个 Release 的其他版本组合。
5. 浏览器按需获取完整 diff 或仅包含变更章节的精简结果。

下载源为：

```text
https://www.3gpp.org/ftp/Specs/archive
```

这里使用的是 3GPP 官方 archive 的 HTTPS 接口，而不是传统的 `ftp://` 连接。

## 环境要求

- Python 3.10+
- `curl`
- LibreOffice：转换旧 DOC，并优先用于生成 WMF/EMF 预览
- Inkscape：可选，作为矢量图预览转换的后备方案
- Node.js：仅在需要运行 Playwright 前端检查时使用，服务运行本身不依赖 Node.js

Debian/Ubuntu 可以先安装系统依赖：

```bash
sudo apt update
sudo apt install curl libreoffice inkscape
```

## 安装

```bash
git clone https://github.com/flintt/3gpp_diff.git
cd 3gpp_diff

python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## 启动

生产式启动：

```bash
./start.sh
```

默认监听 `0.0.0.0:5001`。开发或快速调试也可以直接运行：

```bash
python3 app.py
```

打开 <http://localhost:5001/>，在右上方输入规范号并点击 **Download**。第一次解析或生成图片预览可能较慢，后续请求会直接使用缓存。

## 配置

| 环境变量 | 默认值 | 作用 |
| --- | ---: | --- |
| `PORT` | `5001` | HTTP 监听端口 |
| `WEB_WORKERS` | `2` | Gunicorn worker 数量 |
| `WEB_THREADS` | `4` | 每个 worker 的线程数 |
| `WEB_TIMEOUT` | `180` | 请求超时秒数 |
| `WEB_GRACEFUL_TIMEOUT` | `30` | Gunicorn 优雅退出时间 |
| `WEB_KEEP_ALIVE` | `5` | HTTP keep-alive 秒数 |
| `VECTOR_PREVIEW_DPI` | `200` | 矢量图片预览 DPI |
| `CACHE_SCHEMA_RETENTION` | `2` | 保留最近几代解析/diff 缓存，范围 1–5 |

例如：

```bash
PORT=8080 WEB_WORKERS=1 WEB_THREADS=8 VECTOR_PREVIEW_DPI=240 ./start.sh
```

## Cloudflare Tunnel

本地服务启动后，可以使用 Cloudflare Tunnel 暴露端口：

```bash
cloudflared tunnel --url http://localhost:5001
```

前端 JS/CSS 使用内容指纹路径；新版本会产生新的 URL，避免浏览器或 Cloudflare 继续显示旧文件。HTML 和动态 API 明确禁止 CDN 缓存，指纹静态资源则允许长期缓存。

应用自身没有账号、登录或权限控制。如果实例暴露到公网，建议在 Cloudflare Access 或上游反向代理中增加访问保护。

## 缓存目录

运行数据全部写入 `cache/`，该目录不会提交到 Git：

```text
cache/
├── 23_501/                 # ZIP 和解压后的 DOC/DOCX
├── parsed/v*/23.501/       # 结构化章节解析缓存
├── diffs/v*/23.501/        # 完整与 changes-only diff 缓存
├── images/23.501/          # 原始矢量图和 PNG 预览
├── tasks/                  # 后台下载/预计算任务状态
└── app.log                 # 应用日志
```

缓存均可重新生成。解析器或 diff 输出格式变化时，代码会提升 schema 并自动使用新目录；启动脚本会清理超出保留代数的旧缓存。

## API 概览

| Endpoint | 用途 |
| --- | --- |
| `GET /api/specs` | 列出本地已有规范 |
| `GET /api/versions?spec=23.501` | 列出已缓存版本 |
| `POST /api/download` | 后台下载规范基线版本 |
| `GET /api/download-status` | 查询下载进度 |
| `GET /api/diff` | 获取完整或 changes-only diff |
| `GET /api/diff-stream` | 通过 SSE 获取比较进度 |
| `GET /api/diff-search` | 搜索缓存 diff 正文 |
| `POST /api/precompute` | 触发版本组合预计算 |
| `GET /api/diff-coverage` | 查看版本组合缓存覆盖情况 |
| `GET /api/image/...` | 显示预览图或下载原始图片 |

比较示例：

```bash
curl --compressed \
  'http://localhost:5001/api/diff?spec=33.122&v1=18.0.0&v2=19.0.0&view=changes'
```

## 项目结构

```text
app.py                          Flask API、任务协调和磁盘缓存
spec_fetcher.py                 3GPP 版本发现、下载与解压
spec_parser.py                  Word 章节、表格和图片解析
diff_engine.py                  章节匹配、移动识别和 diff 树
libreoffice_image_converter.py  LibreOffice 矢量图预览转换
static/                         HTML、CSS 和浏览器端交互
tests/                          unittest 回归测试
start.sh                        Gunicorn 启动脚本
```

## 测试

运行完整回归测试：

```bash
python3 -m unittest discover -s tests -v
```

检查前端 JavaScript 语法：

```bash
node --check static/js/app.js
```

安装可选的浏览器检查依赖：

```bash
npm install
npx playwright install chromium
```

## 当前限制

- 下载器默认只自动获取 Release 的 `.0.0` 基线版本；其他维护版本需要已有本地缓存或额外扩展下载策略。
- 复杂的纵向合并、嵌套 Word 表格会按简化结构展示。
- WMF/EMF 不能由主流浏览器原生显示，因此页面使用 PNG 预览，同时提供原始矢量文件下载。
- 大型规范第一次解析和第一次生成版本组合 diff 会消耗较多时间和磁盘空间。

