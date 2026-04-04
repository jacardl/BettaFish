# web-access Skill（qclaw 适配版）

> 源自 [eze-is/web-access](https://github.com/eze-is/web-access)，MIT 协议。

## 快速开始

### 1. 检查 Chrome 远程调试

在 Chrome 地址栏打开：

```
chrome://inspect/#remote-debugging
```

勾选 **"Allow remote debugging for this browser instance"**，然后重启 Chrome。

### 2. 启动 CDP Proxy

```bash
node "C:\Users\jacar\.qclaw\workspace\skills\web-access\scripts\cdp-proxy.mjs"
```

看到 `[CDP Proxy] 运行在 http://localhost:3456` 即表示启动成功。

### 3. 开始使用

CDP Proxy 启动后，你可以通过 HTTP API 操作 Chrome：

```bash
# 新建 tab 打开网页
curl -s "http://localhost:3456/new?url=https://www.xiaohongshu.com"

# 执行 JS 读取内容
curl -s -X POST "http://localhost:3456/eval?target=TARGET_ID" -d "document.title"

# 截图
curl -s "http://localhost:3456/screenshot?target=TARGET_ID&file=C:/tmp/shot.png"

# 点击元素
curl -s -X POST "http://localhost:3456/click?target=TARGET_ID" -d "button.submit"

# 关闭 tab
curl -s "http://localhost:3456/close?target=TARGET_ID"
```

## 目录结构

```
web-access/
├── SKILL.md                      # Skill 主文件（AI 使用指南）
├── README.md                     # 本文件（用户快速开始）
├── .gitignore
├── scripts/
│   └── cdp-proxy.mjs             # CDP Proxy 主脚本
└── references/
    ├── cdp-api.md                # CDP API 详细参考
    └── site-patterns/            # 站点经验积累（按域名）
        └── *.md
```

## 适用场景

- 小红书、微信公众号等反爬平台的内容抓取
- 需要登录态才能访问的页面操作
- 需要模拟用户交互（点击、滚动、填表）的任务
- 视频截帧分析
- 竞品调研、舆情监控等批量网页操作
