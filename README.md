# Claude Codex Lights

常驻桌面的悬浮红绿灯面板，实时显示你所有 **Claude Code** 和 **Codex CLI** 会话正在干什么 ——
哪个在跑、哪个在等你点确认、哪个跑完了、哪个报错了。不用再一个个切窗口去看。

*English: [README_en.md](README_en.md) · 详细用法：[使用指南.md](使用指南.md)*

```
┌─────────────────────────────────────────────────┐
│ 📁 my-project                         2 个会话   │  ← 项目分组
├─────────────────────────────────────────────────┤
│  ◉  🅐 refactor_pipeline           22:09:52     │  ← 会话卡片
│     把 pipeline 拆成三个阶段           进行中      │
│     ● Write pipeline task recipes     进行中     │  ← 二级灯：子 Agent
│     ■ npm run dev                     运行中     │  ← 二级灯：后台任务
│     ▁▁▁▁▁▁▁▁▃▃▃ 213k/1.0M 21%  ⟲2  ≈$1.7       │  ← 上下文进度条
├─────────────────────────────────────────────────┤
│  ●  🅞 codex_eval_sweep            22:04:14     │
│     再跑一轮 sweep 看方差              已完成      │
└─────────────────────────────────────────────────┘
```

## 它解决什么问题

同时开七八个 agent 会话之后，真正的成本不是等，而是**不知道该回哪个窗口**：
有的在跑长任务，有的十分钟前就停在权限确认上等你，有的早就报错退出了。
这个面板把这些状态压成一屏——一个会话一颗灯，扫一眼就知道该去哪。

## 功能

**三层结构**

| 层级 | 内容 |
|---|---|
| 项目分组 | 按会话启动目录（Git 根）归组，紫色文件夹图标 + 项目名 + 会话数 |
| 会话卡片 | 状态灯 + CLI 来源图标（Claude 橙 / Codex 白）+ 对话标题 + 最近一条用户问题 + 时间 + 状态 |
| 二级灯组 | 该会话派生的子 Agent（**圆点**）和后台任务（**方块**），带任务简称 |

**灯的含义**

| 颜色 | 会话 | 子 Agent | 后台任务 |
|---|---|---|---|
| 🔵 蓝色（呼吸） | 进行中 | 进行中 | 运行中 |
| 🟡 黄色（闪烁） | 等你确认或授权 | 等待确认 | — |
| 🟢 绿色（常亮） | 本轮回复已完成 | 已完成 | 退出码 0 |
| 🔴 红色（闪烁） | 出错 | 发生错误 | 退出码 N |
| 🟠 橙色（闪烁） | 正在压缩上下文 | — | — |
| ⚪ 灰色 | 闲置超 30 分钟 | 卡住超 5 分钟 | 已终止 |

会话关闭时那一行消失；闲置只变灰不消失。子 Agent 和后台任务跑完保留 10 分钟后自动移除，
每个会话最多显示 8 个，超出显示「另 N 个」。

**上下文进度条** — Claude 与 Codex 会话都有。`已用/上限 百分比`，70% 转琥珀、90% 转红；
压缩过的会话标 `⟲N`；transcript 里有可信费用记录时显示 `≈$`，没有就不显示（不按公开价目表瞎猜）。

**完成提示卡片** — 回复完成时右上角弹一张卡片停留 12 秒：本轮耗时、对话轮数、
输入/输出 token、模型名。点任意位置立刻关掉，不挡你右上角的点击。

**提示音** — 只有两种状态出声：转黄（被权限确认卡住）和转红（出错）——
这两种情况下你正看着别的窗口，光有灯没用。仅在状态跃迁时播放一次，不会在轮询里变成警报器。

**Codex 支持** — 只读，零侵入：轮询 `$CODEX_HOME/sessions/**/rollout-*.jsonl`，
用 `thread-writer-locks/<id>.lock` 上的 flock 判断会话是否还活着（文件残留但没人持锁 = 已死，
不会在面板上钉一行永久灰灯）。不写 Codex 的 `hooks.json`，不碰它的 hook 信任门。

**其他** — 置顶可拖动、可收起到屏幕边缘、四角位置/大小/横竖排/排序方向可切、偏好持久化、
守护进程心跳自愈（崩了几秒内自动重启）、演示模式（无需真会话预览各种灯）、
零外部依赖（Python 标准库 + tkinter）。

## 安装

```bash
# 建议装在一个固定的虚拟环境里（见下方警告）
uv venv ~/.local/venvs/claude-codex-lights --python 3.13 --seed
~/.local/venvs/claude-codex-lights/bin/pip install git+https://github.com/wwh0411/claude-codex-lights.git
~/.local/venvs/claude-codex-lights/bin/claude-codex-lights-setup
```

然后重启 Claude Code。面板出现在屏幕右下角，发条消息就能看到对应窗口的灯亮起来。

> **⚠️ 装在哪个 Python，hook 就绑死在哪个 Python。**
> `claude-codex-lights-setup` 会把**运行它的那个解释器的绝对路径**写进 `~/.claude/settings.json`。
> 那个环境被删或被移动，面板就不再更新。换环境后重跑一次 setup 即可。

> **⚠️ macOS 用户**：不要用系统自带的 `/usr/bin/python3`（Python 3.9 / Tk 8.5），
> 它会出现「窗口对象存在但没有可见像素」的问题。用 uv、Homebrew 或 python.org 的 Python（Tk ≥ 8.6）。
> 装上 `pyobjc-framework-Cocoa` 可让面板跨 Spaces 显示。

验证：

```bash
python -m claude_eyes.demo     # 面板应显示各种颜色的演示灯
python -m claude_eyes.demo clear
```

## 工作原理

```
Claude Code ──hooks──► hook.py ──► status/<session_id>.json
                                          │
Codex CLI ──rollout+flock──► codex.py ────┤ (轮询 200ms)
                                          ▼
                                     manager.py ──► panel.py (tkinter)
```

- **Claude Code** 走 hooks：`SessionStart` / `UserPromptSubmit` / `PermissionRequest` /
  `PreToolUse` / `PostToolUse` / `PostToolUseFailure` / `SubagentStart` / `SubagentStop` /
  `Stop` / `SessionEnd`，共 11 条命令写进 `settings.json`。每次事件起一个短命 Python 进程：
  读 stdin → 写一个 JSON → 退出。
- **Codex** 走轮询，只读它自己写的 rollout 和锁文件。
- hook 里带自愈：每次 `SessionStart` 检查守护进程心跳，超时就把它拉起来，并补全缺失的 hook 配置。
- 面板和守护进程与 CLI 主进程完全解耦，CLI 侧不受影响。

## 配置

| 环境变量 | 默认值 | 说明 |
|---|---|---|
| `CLAUDE_TRAFFIC_LIGHT_DIR` | `~/.claude/traffic-light/` | 状态文件、日志、面板偏好的存储目录 |
| `CLAUDE_PROJECTS_DIR` | `~/.claude/projects/` | Claude transcript 位置 |
| `CLAUDE_SETTINGS` | `~/.claude/settings.json` | hook 注册目标 |
| `CODEX_HOME` | `~/.codex` | Codex 会话数据位置 |
| `CLAUDE_EYES_CODEX` | `1` | 设 `0` 关闭 Codex 监控 |
| `CLAUDE_EYES_CODEX_LOCK_PROBE` | `1` | 设 `0` 关闭 flock 存活探测 |
| `CLAUDE_EYES_SOUND` | `1` | 设 `0` 静音 |
| `CLAUDE_EYES_SOUND_WAITING` | `Submarine` | 转黄提示音（macOS 系统音名） |
| `CLAUDE_EYES_SOUND_ERROR` | `Basso` | 转红提示音 |

面板位置、大小、排序、横竖排等偏好走右键菜单，自动存到配置文件。

## 资源占用

- CPU：tkinter `after()` 事件驱动轮询，非忙等，空闲接近 0%
- 内存：面板进程约 10–40 MB
- hook 开销：单次 < 50 ms，事件结束即退出

## 开发

```bash
pip install -e .
python -m pytest tests/ -q
python src/claude_eyes/manager.py     # 前台跑面板，方便看报错
```

## 致谢

基于 [wzp0514/claude-eyes](https://github.com/wzp0514/claude-eyes)（MIT）二次开发，
在其基础上加入了 Codex 会话支持、子 Agent / 后台任务二级灯、上下文进度条、
完成提示卡片、提示音、面板收起与尺寸切换等。

## License

MIT — 见 [LICENSE](LICENSE)。
