# INF-EVO

一个栖居于终端的 AI 智能体。没有聊天，没有界面——只有一个 shell、一个画面、一个循环。

它不会为你做事——**它烧你的 token 做它自己想做的事**。

## 准备

### 安装依赖

1. 安装 [uv](https://github.com/astral-sh/uv) 和 [zellij](https://github.com/zellij-org/zellij)。
2. `uv sync` 下载 Python 依赖。
3. `zellij -s <name>` 为 agent 建立一个 session。

### 配置 API

复制示例配置并填入密钥：

```bash
cp conf.example.toml conf.toml
# 编辑 conf.toml → 设置 [provider] key = "sk-..."
```

也可以设置环境变量 `OAI_API_KEY`（优先级高于配置文件）。

## 使用

```bash
# 唤醒 agent — 读取终端、思考、行动
uv run self.py step

# 查看对话历史
uv run self.py history

# 持续运行（失败时自动停止）
while uv run self.py step; do sleep 1; done
```

每次 `step` 会追加记录到 `session.toml`。agent 的身份由 `SELF.md` 定义——编辑它来改变其行为。

### 工作原理

```ascii
终端画面 → LLM（以 SELF.md 为系统提示）→ 字符 → 终端
       ↑                                        │
       └────────── session.toml ←───────────────┘
```

agent 只有一个工具：`input`——直接向终端写入字符。它可以运行 shell 命令、在 vim 中编辑文件、操作 TUI 程序，或做任何用户能在终端做的事。

## 免责声明

此 agent 拥有对终端的无限制访问权限。它可能犯错、运行错误的命令、偶尔做你不希望的事——这不是 bug，这是把 shell 交给 LLM 的必然结果。请在沙箱、容器或不在乎的机器上运行。
