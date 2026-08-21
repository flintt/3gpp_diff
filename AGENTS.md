# 3GPP Delta Agent 操作手册

本文档适用于整个仓库，目标是让新接手的 Agent 能快速部署、运行、定位问题并安全开发。面向普通使用者的介绍见 `README.md`；实现细节以代码和测试为准。`plan.md` 是早期改进计划，其中多项内容已经完成，不能直接当作当前待办清单。

## 1. 项目概要

这是一个本地化的 3GPP Word 规范版本对比工具：

1. `spec_fetcher.py` 从 3GPP 官方 HTTPS archive 发现并下载规范 ZIP。
2. `spec_parser.py` 将 DOC/DOCX 解析为章节树，并提取表格和图片。
3. `diff_engine.py` 匹配两个版本的章节，包括重编号、跨章节移动和正文下沉。
4. `app.py` 提供 Flask API、后台任务、内存/磁盘缓存和静态页面。
5. `static/` 中的原生 HTML、CSS、JavaScript 展示 Split/Inline diff。

项目没有数据库，也没有前端打包步骤。运行数据全部在仓库根目录的 `cache/` 中；该目录可重新生成且不能提交到 Git。

## 2. 接手后的第一轮检查

始终从仓库根目录工作：

```bash
cd /path/to/3gpp_diff
git status --short
git log -5 --oneline
python3 --version
```

先确认工作树是否已有用户改动。不要覆盖、还原或顺带提交不属于当前任务的修改。

快速验证代码基线：

```bash
python3 -m unittest discover -s tests -v
node --check static/js/app.js
```

Node.js 只用于 JavaScript 语法和可选的 Playwright 浏览器检查；应用本身不依赖 Node.js。

## 3. 首次安装

系统依赖：

- Python 3.10 或更高版本；
- `curl`，用于访问 3GPP archive；
- LibreOffice，旧 DOC 转 DOCX 以及 WMF/EMF 预览优先使用它；
- Inkscape，可选，作为矢量图转换后备；
- Node.js，可选，仅用于前端检查。

Debian/Ubuntu 示例：

```bash
sudo apt update
sudo apt install curl libreoffice inkscape

python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -r requirements.txt
```

需要浏览器自动化时再安装：

```bash
npm install
npx playwright install chromium
```

不要把虚拟环境、浏览器文件、下载的规范或生成缓存加入 Git。

## 4. 启动与停止

### 开发运行

```bash
source .venv/bin/activate
PORT=5001 python3 app.py
```

### 常规部署运行

```bash
source .venv/bin/activate
./start.sh
```

`start.sh` 会先清理过期 schema 缓存，再以 Gunicorn `gthread` 模式启动。默认配置为 2 个 worker、每个 worker 4 个线程，监听 `0.0.0.0:5001`。

必须从仓库根目录启动，因为 `static/` 和多个 `cache/` 路径是相对当前工作目录解析的。服务进程必须对 `cache/` 有读写权限。

停止前台服务使用 `Ctrl-C`。由 systemd、Supervisor 或容器管理时，应通过对应管理器重启，不要同时手动启动第二套实例。

### 更新已有部署

先识别当前进程由谁管理，并确认工作目录、运行用户和虚拟环境。不要在已有 Gunicorn 旁边直接再执行一次 `start.sh`。

在用户已经授权部署且工作树干净的前提下，通常按以下顺序更新：

```bash
git status --short
git pull --ff-only
source .venv/bin/activate
python3 -m pip install -r requirements.txt
python3 -m unittest discover -s tests -q
node --check static/js/app.js
```

然后使用现有的 systemd、Supervisor 或容器管理器重启服务，并执行下文的部署检查。不要删除 `cache/`；它应作为部署间保留的可写数据目录。只有 `requirements.txt` 变化时才需要重新安装 Python 依赖，只有 `package.json` 或 `package-lock.json` 变化且确实需要浏览器检查时才需要运行 `npm install`。

### 常用环境变量

| 变量 | 默认值 | 作用 |
| --- | ---: | --- |
| `PORT` | `5001` | 监听端口 |
| `WEB_WORKERS` | `2` | Gunicorn worker 数量 |
| `WEB_THREADS` | `4` | 每个 worker 的线程数 |
| `WEB_TIMEOUT` | `180` | 请求超时秒数 |
| `WEB_GRACEFUL_TIMEOUT` | `30` | 优雅退出超时 |
| `WEB_KEEP_ALIVE` | `5` | HTTP keep-alive 秒数 |
| `VECTOR_PREVIEW_DPI` | `200` | WMF/EMF 预览 PNG 的 DPI |
| `CACHE_SCHEMA_RETENTION` | `2` | 保留最近几代解析/diff 缓存，范围 1–5 |

小内存机器可以从以下配置开始：

```bash
WEB_WORKERS=1 WEB_THREADS=4 ./start.sh
```

### 部署检查

应用没有单独的 `/health` endpoint，可以用以下请求检查：

```bash
curl -fsS http://localhost:5001/ >/dev/null
curl -fsS http://localhost:5001/api/specs
```

页面和 API 正常后再配置反向代理或 Tunnel。应用没有登录和权限控制，公网实例必须由 Cloudflare Access 或上游反向代理保护。实际域名、Tunnel Token、证书和访问凭据必须放在仓库外。

## 5. 第一次下载与对比

通过页面右上角输入规范号并点击 Download，或者直接调用 API：

```bash
curl -fsS -X POST \
  -H 'Content-Type: application/json' \
  -d '{"spec":"23.501"}' \
  http://localhost:5001/api/download

curl -fsS 'http://localhost:5001/api/download-status?spec=23.501'
curl -fsS 'http://localhost:5001/api/versions?spec=23.501'
```

下载任务只选择 Release 15 及以上的 `.0.0` 基线版本。下载完成后会自动预计算最近六个 Release 的全部版本组合，并优先处理相邻 Release。

已有两个版本后可检查 diff：

```bash
curl --compressed -fsS \
  'http://localhost:5001/api/diff?spec=23.501&v1=18.0.0&v2=19.0.0&view=changes' \
  >/tmp/3gpp-diff-smoke.json
```

`view=changes` 省略未变化章节的正文，适合快速检查；`view=full` 返回完整结果。`refresh=1` 会强制重新计算，日常请求不要滥用。

下载和首次解析可能耗时较长，这是正常现象。不要把真实网络下载作为单元测试前置条件。

## 6. 数据与缓存

主要目录如下：

```text
cache/
├── 23_501/                 # 原始 ZIP、解压后的 DOC/DOCX
├── parsed/v*/23.501/       # 解析后的章节树
├── diffs/v*/23.501/        # gzip 压缩的完整/changes-only diff
├── images/23.501/          # 原始 WMF/EMF 和 PNG 预览
├── tasks/                  # 多 worker 可见的任务状态和锁
└── app.log                 # 运行日志
```

重要规则：

- 修改解析输出或章节树语义时，提升 `app.py` 中的 `PARSED_CACHE_SCHEMA`。
- 修改 diff 匹配、状态、字段或序列化语义时，提升 `DIFF_CACHE_SCHEMA`。
- 只修改样式或前端交互不需要提升上述 schema。
- 前端资源指纹在 `app.py` 导入时计算；修改 HTML、CSS 或 JS 后必须重启服务。
- 不要用“每次请求全部重算”规避缓存问题。先确认源文件 identity、schema 和磁盘缓存失效逻辑。
- 不要随意删除整个 `cache/`。优先删除明确的规范或旧 schema 目录；缓存可能很大且重新生成昂贵。

启动时 `prune_obsolete_caches()` 按 `CACHE_SCHEMA_RETENTION` 清理旧解析/diff schema。缓存写入、下载发布和任务状态使用临时文件加原子替换；并发修改时必须保留这种特性。

## 7. 代码结构与修改入口

| 文件 | 负责内容 | 常见修改场景 |
| --- | --- | --- |
| `app.py` | Flask 路由、缓存、SSE、后台任务、多 worker 协调 | API、缓存、任务、响应头 |
| `spec_fetcher.py` | 版本发现、ZIP 下载、文档解压 | 下载源、版本规则、原子下载 |
| `spec_parser.py` | DOC/DOCX、章节、表格、图片解析 | 表格错位、标题层级、图片提取 |
| `libreoffice_image_converter.py` | LibreOffice 矢量图批量预览转换 | WMF/EMF 预览质量和兼容性 |
| `diff_engine.py` | 章节匹配、移动/重编号识别、统计 | Added/Deleted 误判、章节移动 |
| `static/index.html` | 页面结构 | 控件和语义结构 |
| `static/css/main.css` | 亮/暗主题和响应式布局 | 样式、表格、diff 高亮 |
| `static/js/app.js` | 页面状态、API、版本选择、渲染 | Split/Inline、搜索、tooltip |
| `tests/` | unittest 回归测试 | 所有后端和关键前端缓存行为 |

### 数据流

```text
3GPP archive
  → ZIP/DOC/DOCX
  → parse_spec() 章节树、表格、图片
  → parsed/v* 磁盘缓存
  → diff_trees() 匹配两个章节树
  → diffs/v* gzip 缓存
  → Flask API/SSE
  → 浏览器 Split 或 Inline 渲染
```

## 8. 开发约束

### 章节匹配

- 匹配规则必须通用，不能为某个规范号或某个章节写硬编码特例。
- 优先使用结构、标题、正文相似度、相对编号和唯一性作为证据。
- 模糊时宁可保留 Added/Deleted，也不要把两个无关章节强行匹配。
- 修复重编号或移动问题时，先构造最小章节树回归测试，再修改算法。
- `old_id`、`change_type` 和 `status` 会被前端用于 Inline/Split 展示，修改字段时同步检查前端。

### 文档解析

- DOC 必须先通过隔离的 LibreOffice profile 转为 DOCX，避免并发 profile 锁冲突。
- 一个 ZIP 可能包含多个文档分卷；必须保持自然文件名顺序并合并解析结果。
- 表格行应保持矩形；简单横向合并需要展开。复杂纵向合并和嵌套表格目前允许简化展示。
- WMF/EMF 原文件应保留，浏览器显示 PNG 预览。优先 LibreOffice，失败后再走 Inkscape。
- 修复解析问题时，不要把完整 3GPP 文档加入测试或提交；用最小内存 fixture 或测试临时文件。

### 后端并发

- Gunicorn 多 worker 之间不共享 Python 内存。
- 后台任务状态和锁必须写入 `cache/tasks/`，不能只依赖进程内字典。
- 相同版本组合的并发请求应共享一次计算，等待锁后必须再次检查缓存。
- 磁盘缓存失败不应让一个本来可返回的在线比较失败。
- Linux 部署使用 `fcntl` 文件锁；如果改变跨平台行为，需要补充对应测试和降级路径。

### 前端

- 没有 bundler；直接编辑 `static/index.html`、`static/css/main.css`、`static/js/app.js`。
- 所有用户输入进入 URL 时使用 `URLSearchParams` 或等价编码方式。
- 修改版本选择、过滤或 Inline/Split 状态时，要同时验证正常视图和搜索过滤视图。
- 大文档要避免把完整 diff 反复传输或重复插入 DOM；优先使用 changes-only、搜索索引和现有缓存接口。
- 修改亮色主题时必须同时检查表格、tooltip、Added/Deleted/Modified 高亮的可读性。

## 9. 测试策略

每次代码修改至少运行：

```bash
python3 -m unittest discover -s tests -v
node --check static/js/app.js
git diff --check
```

也可以只跑相关文件加快循环：

```bash
python3 -m unittest tests.test_diff_engine -v
python3 -m unittest tests.test_spec_parser -v
python3 -m unittest tests.test_spec_fetcher -v
python3 -m unittest tests.test_app -v
```

提交前仍需运行完整测试。测试日志中可能故意出现 ERROR/WARNING，用于覆盖下载失败、损坏缓存和只读目录等路径；最终 `OK` 和退出码才是判断依据。

涉及 UI 的修改还需手动验证：

1. 亮色与暗色主题；
2. Split 与 Inline；
3. 正常目录与搜索过滤状态；
4. 至少两个不同版本组合；
5. 表格、图片、移动/重编号章节；
6. 桌面宽屏和窄屏布局。

## 10. 常见故障定位

### 页面仍然是旧版本

先确认进程已经重启。前端指纹只在应用导入时生成。然后检查 HTML 是否引用了新的 `/assets/<fingerprint>/...` 地址；动态 API 和 HTML 应禁止 CDN 缓存，带指纹资源允许长期缓存。必要时再清理 CDN 缓存。

### 有 ZIP，但版本列表为空

检查文件名是否能被 `spec_fetcher.py` 解析、ZIP 是否完整，以及 Release 是否低于 15。`/api/versions` 只列本地已缓存版本。

### DOC 或图片转换失败

检查：

```bash
command -v libreoffice
command -v inkscape
libreoffice --version
```

再查看 `cache/app.log`。确认服务用户可写系统临时目录、规范解压目录和 `cache/images/`。

### 章节被错误显示为删除加新增

先分别调用 `/api/parse` 查看两个版本的章节树：如果树本身错误，修 `spec_parser.py`；如果树正确但配对错误，修 `diff_engine.py`。为最小结构添加通用回归测试，提升相应 cache schema，并用 `refresh=1` 或新 schema 验证。

### 表格显示为凌乱文字

先判断源 DOCX 中是否真的是 Word table。检查单元格段落、grid span、纵向合并和嵌套表格；不要仅根据正文中出现单词 “Table” 推断表格结构。

### diff 很慢

检查 `/api/diff-coverage` 和 `cache/diffs/v*/`，确认是否已有对应组合。首次解析和首次组合计算昂贵，命中磁盘缓存后应明显加快。不要在前端默认加 `refresh=1`。

## 11. 安全、隐私与版权

- 不在代码、README、日志示例或提交信息中写真实域名、个人邮箱、本机用户名、绝对 home 路径、Token、Cookie、密码或私钥。
- 提交作者使用平台提供的 noreply 邮箱；Git 元数据同样是公开内容。
- `.env`、证书、Tunnel 配置和服务管理器的环境文件留在仓库外。
- `cache/` 可能包含下载的版权文稿、解析内容和访问日志，不得提交或在问题报告中整段粘贴。
- 调试输出只保留复现所需的最小片段，并在分享前脱敏。
- 3GPP 文稿版权归相应权利人所有；测试 fixture 必须最小化，不复制大段规范正文。

提交前至少执行：

```bash
git status --short
git diff --check
git diff --staged
git grep -n -I -E 'BEGIN .*PRIVATE KEY|api[_-]?key|access[_-]?token|password' -- .
```

专用 secret scanner 可用时，还应扫描当前工作树和完整 Git 历史。发现真实凭据后先轮换凭据，再讨论历史重写；不要未经确认 force-push。

## 12. 完成任务前的交付清单

1. 确认没有覆盖用户原有修改。
2. 为行为变化添加或更新回归测试。
3. 必要时提升 parsed/diff cache schema。
4. 运行完整 unittest、JavaScript 语法检查和 `git diff --check`。
5. 检查 `git diff`，确保没有缓存、下载文稿、日志、凭据或个人信息。
6. 明确告诉用户是否需要重启服务、重新计算缓存或清理 CDN。
7. 只有用户明确要求时才提交和推送；重写历史或 force-push 必须单独确认。
