---
name: web-access
license: MIT
github: https://github.com/eze-is/web-access
description: 完整联网能力 Skill。处理所有联网操作：搜索、网页抓取、登录后操作、网络交互、浏览器自动化。
metadata:
  author: 一泽Eze (ported to qclaw)
  version: 2.4.1-qclaw
---

# web-access — qclaw 适配版

> 源自 [eze-is/web-access](https://github.com/eze-is/web-access)，MIT 协议。

## 能力概览

| 场景 | 工具 | 说明 |
|------|------|------|
| 搜索摘要、关键词发现 | `web_search` | 搜索发现信息来源 |
| URL 已知，定向提取内容 | `web_fetch` | 拉取网页，由小模型提取，返回处理后结果 |
| 原始 HTML（meta、JSON-LD） | `exec` + `curl` | 获取结构化字段 |
| 非公开内容 / 反爬限制平台（小红书等） | CDP 浏览器 | 直连 Chrome，绕过静态层 |
| 需要登录态、交互操作 | CDP 浏览器 | 携带用户登录态操作页面 |

## 前提：环境检查

使用 CDP 之前，先检查 Node.js 和 Chrome 调试端口：

```bash
node --version   # 需要 v22+
```

Chrome 调试端口检查（在 Chrome 地址栏打开）：

```
chrome://inspect/#remote-debugging
```

勾选 **"Allow remote debugging for this browser instance"**（可能需要重启浏览器）。

Windows 上常用调试端口：9222、9229、9333。

### 快速启动 CDP Proxy

CDP Proxy 脚本路径：

```
C:\Users\jacar\.qclaw\workspace\skills\web-access\scripts\cdp-proxy.mjs
```

启动命令：

```bash
node "C:\Users\jacar\.qclaw\workspace\skills\web-access\scripts\cdp-proxy.mjs"
```

Proxy 运行后通过 HTTP API 控制 Chrome，默认端口 `3456`。

## 浏览哲学

**目标驱动，而非步骤驱动。** 像人一样思考，兼顾高效与适应性。

1. **明确目标** — 先定义成功标准：什么算完成了？需要获取什么信息、执行什么操作？
2. **选择起点** — 根据任务性质选最可能直达的方式。一次不成功则调整方向。
3. **过程校验** — 每一步结果都是证据。用结果对照目标：路径在推进吗？发现方向错了立即调整。
4. **完成判断** — 对照成功标准确认完成，不要为"完整"而浪费代价。

## CDP Proxy HTTP API

Proxy 地址：`http://localhost:3456`

### 端点列表

| 端点 | 方法 | 说明 |
|------|------|------|
| `/health` | GET | 健康检查，返回连接状态 |
| `/targets` | GET | 列出所有已打开的页面 tab |
| `/new?url=URL` | GET | 创建新后台 tab（自动等待加载） |
| `/close?target=ID` | GET | 关闭指定 tab |
| `/navigate?target=ID&url=URL` | GET | 在已有 tab 中导航（自动等待加载） |
| `/back?target=ID` | GET | 后退一页 |
| `/info?target=ID` | GET | 获取页面信息（title、url、readyState） |
| `/eval?target=ID` | POST | 执行 JavaScript（POST body 为 JS 代码） |
| `/click?target=ID` | POST | JS 层面点击（POST body 为 CSS 选择器） |
| `/clickAt?target=ID` | POST | 真实鼠标点击（CDP Input.dispatchMouseEvent） |
| `/setFiles?target=ID` | POST | 文件上传（POST body 为 JSON） |
| `/scroll?target=ID&y=3000` | GET | 滚动页面（direction: down/up/top/bottom） |
| `/screenshot?target=ID&file=PATH` | GET | 截图（保存到本地文件） |

### curl 调用示例

```bash
# 健康检查
curl -s http://localhost:3456/health

# 列出已打开的 tab
curl -s http://localhost:3456/targets

# 新建后台 tab 并打开网页
curl -s "http://localhost:3456/new?url=https://www.xiaohongshu.com"

# 执行 JS 读取页面标题
curl -s -X POST "http://localhost:3456/eval?target=TARGET_ID" -d "document.title"

# 点击元素
curl -s -X POST "http://localhost:3456/click?target=TARGET_ID" -d "button.submit"

# 真实鼠标点击（可触发文件对话框）
curl -s -X POST "http://localhost:3456/clickAt?target=TARGET_ID" -d "button.upload"

# 文件上传
curl -s -X POST "http://localhost:3456/setFiles?target=TARGET_ID" -d "{\"selector\":\"input[type=file]\",\"files\":[\"C:/path/to/file.png\"]}"

# 截图
curl -s "http://localhost:3456/screenshot?target=TARGET_ID&file=C:/tmp/shot.png"

# 滚动到页面底部
curl -s "http://localhost:3456/scroll?target=TARGET_ID&direction=bottom"

# 关闭 tab
curl -s "http://localhost:3456/close?target=TARGET_ID"
```

## CDP 操作技巧

### /eval — 核心操作
- **看**：查询 DOM，发现页面上的链接、按钮、表单、文本内容
- **做**：点击元素、填写输入框、提交表单
- **读**：提取文字内容，判断图片/视频是否承载核心信息

### 判断用哪种工具

| 情况 | 推荐方式 |
|------|----------|
| 程序化方式可行（URL 可直接访问） | `web_fetch` 或 `exec` + `curl` |
| 目标有反爬限制 | CDP 直接访问 |
| 需要登录态 | CDP（携带用户 Chrome 登录态） |
| 需要交互操作（点击、滚动、填表） | CDP |
| 需要视频当前帧截图 | CDP（天然捕获渲染状态） |

### GUI vs 程序化

- **程序化**（构造 URL 直接导航、eval 操作 DOM）：速度快、精确，但更容易触发反爬
- **GUI 交互**（点击按钮、填写输入框、滚动浏览）：确定性最高，是反爬场景的可靠兜底

根据实际情况判断，程序化受阻时回退到 GUI 交互。

### 常见陷阱

- **小红书等平台**：内容可能被懒加载或需要滚动才展示，先 `/scroll` 再提取
- **平台返回"内容不存在"**：可能是访问方式问题（URL 缺失参数、触发反爬），不一定代表内容真的不存在
- **短时间内大量 /new**：可能触发反爬风控，注意控制节奏

### 截取视频帧

Chrome 渲染 + `/screenshot` 可捕获当前视频帧。通过 `/eval` 操作 `<video>` 元素（获取时长、seek 到任意时间点），再截图可对视频进行离散采样分析。

## 站点经验积累

操作中积累的站点经验存储在：

```
C:\Users\jacar\.qclaw\workspace\skills\web-access\references\site-patterns\
```

格式：

```markdown
---
domain: example.com
aliases: [示例]
updated: 2026-03-29
---
## 平台特征
架构、反爬行为、登录需求、内容加载方式等事实

## 有效模式
已验证的 URL 模式、操作策略、选择器

## 已知陷阱
什么会失败以及为什么
```

## 参考文档

| 文件 | 何时加载 |
|------|----------|
| `references/cdp-api.md` | 需要 CDP API 详细参考时 |
| `references/site-patterns/{domain}.md` | 确定目标网站后，读取对应站点经验 |

> 本 Skill 内容源自 [eze-is/web-access](https://github.com/eze-is/web-access)，MIT 协议。
