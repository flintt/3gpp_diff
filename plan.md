# 3GPP Specification Diff Tool - 改进计划 (Improvement Plan)

本文档旨在为 3GPP Spec Diff 工具的重构和优化提供详细、可实施的步骤。计划分为四个主要阶段，核心重点为提升前端界面的视觉表现和交互体验（Premium Design），同时加固后端架构的稳定性和性能。

## 阶段一：前端视觉重构与代码拆分解耦 (Frontend UI/UX & Architecture)

**目标**：将杂乱的单文件 HTML 转换为具有现代感、模块化的高质量 Web 界面。
**核心规范**：避免使用基础设计，采用现代高阶美学（暗色调、玻璃质感、微动画、现代排版）。

*   **步骤 1.1：代码文件拆分**
    *   创建 `static/css/main.css`，将原 `index.html` 中的 `<style>` 内容迁移并重构。
    *   创建 `static/js/app.js`，将原 `<script>` 中的逻辑状态和函数分离。
    *   清理 `index.html`，只保留基础的 HTML 语义结构。
*   **步骤 1.2：引入现代设计系统 (Aesthetics)**
    *   引入 Google Fonts 字体（如 `Inter` 或 `Outfit`）替换系统默认字体。
    *   重构色彩变量 (CSS Variables)：采用更高级的暗色模式配色，例如 `#0B0F19`（背景）、加入细腻的渐变与投影。
    *   **微动画 (Micro-animations)**：为所有按钮、表格展开、目录 (TOC) 的切换加入平滑过渡 (`transition: all 0.3s ease`)。
*   **步骤 1.3：交互面板优化**
    *   优化**Diff双栏对比面板**的排版布局，改进背景和高亮颜色的对比度，使其更像现代的代码审查工具。
    *   优化目录栏 (Table of Contents) 的视觉层级：使层级缩进更清晰，差异状态点（Added, Deleted, Modified）更加醒目且现代。
    *   优化加载动画 (Loading Spinner)，设计符合现代审美的占位状态 (Skeleton loader 或精美 Spinner)。

## 阶段二：后端缓存与内存管理优化 (Backend Memory & Cache)

**目标**：解决目前 `_parsed_cache` 和 `_diff_cache` 字典无限增长的问题，防止长周期运行导致内存溢出。

*   **步骤 2.1：引入 LRU 缓存策略**
    *   使用 Python 内置的 `functools.lru_cache` 或第三方库 `cachetools`。
    *   重构 `_get_parsed(spec, version)`，为其应用缓存限制（如只在内存中保留最近访问的 20 个解析结果）。
*   **步骤 2.2：Diff 结果缓存淘汰机制**
    *   限制 `_diff_cache` 的内存占用上限。
    *   将过期的对比结果落盘（写入磁盘的 json 文件），在内存未命中时再从磁盘加载，并在磁盘达到一定容量时执行清理脚本。

## 阶段三：后台任务管理与健壮性提升 (Background Tasks & Robustness)

**目标**：优化当前的 `threading.Thread` 任务，避免并发下载/解析导致系统资源耗尽。

*   **步骤 3.1：重构并发模型**
    *   使用 `concurrent.futures.ThreadPoolExecutor` 限制最大并发工作线程数（如 4 或 8）。
    *   将 `_download_all_releases` 和 `_precompute_diffs` 的逻辑迁移至线程池中进行任务投递。
*   **步骤 3.2：引入标准日志系统**
    *   移除分散在代码各处的 `print()`，使用 Python 的 `logging` 模块。
    *   配置日志级别 (INFO, ERROR, DEBUG) 与格式，并写入 `logs/app.log`。
*   **步骤 3.3：子进程异常处理强化**
    *   为 `spec_fetcher.py` (执行 curl) 和 `spec_parser.py` (执行 libreoffice) 补充重试逻辑 (Retry Mechanism) 和更明确的超时机制，防止僵尸进程。

## 阶段四：部署与工程化完善 (Deployment & Engineering)

**目标**：提升项目在服务器环境中的部署便利性与生产环境性能。

*   **步骤 4.1：依赖管理**
    *   提取并创建 `requirements.txt`（包含 `flask`, `lxml`, `python-docx` 等），规范版本号。
*   **步骤 4.2：生产级 WSGI 服务器**
    *   引入 `gunicorn`，通过多进程模式替换 Flask 的内置 development server。
    *   编写一个示例的 `start.sh` 或 Dockerfile，用于一键构建与启动生产级服务。
*   **步骤 4.3：Type Hints 补充**
    *   在核心模块如 `diff_engine.py` 和 `spec_parser.py` 的函数签名中补充标准类型注解，提高代码维护性。

---
**实施顺序建议**：
建议优先执行**阶段一**，这能在短时间内直观地极大提升系统的用户体验。确认前端结构与视觉表现达标后，依次进行二、三、四阶段的底层加固。
