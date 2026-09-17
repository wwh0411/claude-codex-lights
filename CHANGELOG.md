# Changelog

## 0.2.0 — 2026-09-17

项目更名为 **claude-codex-lights**，并在原 `claude-eyes` 基础上完成以下扩展：

**新增**
- Codex CLI 会话支持：只读轮询 rollout transcript，配合 `thread-writer-locks` 的 flock 做存活判定
- 二级灯组：子 Agent（圆点）与后台任务（方块），带任务简称，完成后保留 10 分钟、每会话最多 8 个
- 上下文进度条：`已用/上限 百分比`，70% 琥珀 / 90% 红，压缩次数 `⟲N`，可信费用记录时显示 `≈$`
- 完成提示卡片：本轮耗时、对话轮数、输入/输出 token、模型名，12 秒后消失，点击立即关闭
- 提示音：仅在转黄（等待确认）与转红（出错）的状态跃迁时各响一次
- CLI 来源图标（Claude / Codex）与项目分组标题
- 面板可收起到屏幕边缘，可切换小/中/大三档尺寸
- 压缩状态橙灯

**改进**
- hook 自愈：`SessionStart` 时校验解释器路径与缺失的 hook 条目并补齐
- 守护进程心跳与自动重启
- 测试扩充到 codex / manager / panel / sound / hook 五个模块

**变更**
- 分发名 `claude-eyes` → `claude-codex-lights`；命令 `claude-eyes(-setup)` → `claude-codex-lights(-setup)`
- Python 导入包仍为 `claude_eyes`，`CLAUDE_EYES_*` 环境变量保持不变（已注册的 hook 命令依赖它们）

## 0.1.0

上游 [wzp0514/claude-eyes](https://github.com/wzp0514/claude-eyes) 初始版本：
Claude Code 四态悬浮指示灯、项目分组、hook 注册与守护进程。
