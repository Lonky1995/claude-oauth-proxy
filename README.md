# claude-oauth-proxy

用 Claude Max/Pro **订阅额度**跑 API 调用的本地代理服务。

---

## 这是什么

Claude 的订阅（Max/Pro）和 API 是两套独立的计费体系。但 Claude Code（官方 CLI）在登录之后，会把你的订阅 OAuth Token 存在本地文件里，而且 Anthropic 允许用这个 Token 直接调用 `/v1/messages` 接口。

这个项目就是利用这个机制，在本地或 VPS 上启动一个 HTTP 服务，把标准的 Anthropic API 请求转发出去，并自动注入 OAuth Token 和必要的请求头。对外表现就是一个兼容 Anthropic SDK 的 API 端点。

**一句话**：你订了 Claude Max，这个代理让你的代码也能用上那份额度，不用单独充 API 余额。

---

## 原理

```
你的代码 / AI Agent
      ↓ 标准 Anthropic API 请求
claude-oauth-proxy（本地 :18789）
      ↓ 注入 OAuth Token + 必要请求头
api.anthropic.com/v1/messages
      ↓ 返回结果
你的代码 / AI Agent
```

核心逻辑只做三件事：
1. 从 `~/.claude/.credentials.json`（Claude Code 登录后自动生成）读取 Token
2. 把客户端的请求体里加上 Claude Code 身份标识（Anthropic OAuth 要求）
3. 通过 TLS 转发给 api.anthropic.com，把响应原样返回

---

## 前置条件

- 已订阅 Claude Max 或 Pro
- 已安装 [Claude Code CLI](https://claude.ai/code) 并执行过 `claude login`
- Python 3.8+（系统自带，一般不需要额外安装）

---

## 快速开始

### 1. 克隆项目

```bash
git clone https://github.com/Lonky1995/claude-oauth-proxy.git
cd claude-oauth-proxy
```

### 2. 启动代理

```bash
python3 proxy.py
```

默认监听 `127.0.0.1:18789`。看到这行说明启动成功：

```
[14:00:00.000] claude-oauth-proxy listening on 127.0.0.1:18789
```

### 3. 验证是否正常

```bash
curl http://127.0.0.1:18789/health
# 返回: {"status":"ok","service":"claude-oauth-proxy"}
```

### 4. 让你的代码用这个代理

把 Anthropic SDK 的 base_url 指向本地代理即可：

**Python**
```python
import anthropic

client = anthropic.Anthropic(
    api_key="any-string",          # 随便填，代理不校验
    base_url="http://127.0.0.1:18789"
)

message = client.messages.create(
    model="claude-opus-4-5",
    max_tokens=1024,
    messages=[{"role": "user", "content": "你好"}]
)
print(message.content[0].text)
```

**Node.js**
```javascript
import Anthropic from "@anthropic-ai/sdk";

const client = new Anthropic({
  apiKey: "any-string",
  baseURL: "http://127.0.0.1:18789",
});

const message = await client.messages.create({
  model: "claude-opus-4-5",
  max_tokens: 1024,
  messages: [{ role: "user", content: "你好" }],
});
console.log(message.content[0].text);
```

**环境变量方式**（推荐，不改代码）
```bash
export ANTHROPIC_BASE_URL=http://127.0.0.1:18789
export ANTHROPIC_API_KEY=any-string
```

---

## 在 VPS 上部署

如果你想在服务器上长期运行（比如跑 AI Agent），推荐用 PM2 守护进程。

### 用 PM2 守护

```bash
# 安装 PM2（如果没有）
npm install -g pm2

# 启动
pm2 start proxy.py --name claude-oauth-proxy --interpreter python3

# 开机自启
pm2 save
pm2 startup
```

### 关于网络代理

如果 VPS 在中国大陆或其他无法直连 api.anthropic.com 的地区，需要配置 HTTP 代理：

```bash
export HTTPS_PROXY=http://127.0.0.1:7891  # 改成你的代理地址
python3 proxy.py
```

proxy.py 会自动通过 CONNECT 隧道走代理访问 Anthropic。

---

## 配置参数

```bash
python3 proxy.py --help

# --port      监听端口，默认 18789
# --bind      监听地址，默认 127.0.0.1（仅本机访问）
# --token-file  credentials 文件路径，默认 ~/.claude/.credentials.json
```

**绑定到 0.0.0.0 允许局域网访问**（注意：不要暴露到公网，Token 没有鉴权）：

```bash
python3 proxy.py --bind 0.0.0.0
```

---

## Token 来源说明

代理按以下优先级读取 Token：

1. **环境变量** `CLAUDE_OAUTH_TOKEN`：适合 CI/CD 或服务器场景，Token 通过 `claude setup-token` 获取（较长效）
2. **credentials 文件** `~/.claude/.credentials.json`：Claude Code 登录后自动写入，会定期刷新

本地开发推荐直接用 credentials 文件，服务器推荐用环境变量。

---

## 注意事项

- 这个代理没有内置鉴权，**不要把端口暴露到公网**
- Token 是你的 Claude 账号凭证，不要分享给他人
- 使用量计入你的 Claude 订阅配额，不是 API 余额
- Anthropic 的订阅条款允许个人通过 OAuth 调用 API，但请勿用于多人共享或商业服务

---

## 给 AI 的上下文

如果你把这个 README 喂给 AI 让它帮你操作，告诉它：

> 我已经订阅了 Claude Max，已经安装了 Claude Code CLI 并登录。我想在 [本地/VPS] 部署 claude-oauth-proxy，让我的 [Python/Node.js/其他] 项目用上订阅额度。服务器系统是 [Ubuntu 22.04/macOS/其他]。

AI 会根据这些信息给出具体的操作步骤。
