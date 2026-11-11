# phone-use (Akashic Android 端侧后台虚拟屏控制插件)

独立外置插件仓库（支持 `plugin-install` 热安装与热重载），为 Akashic Agent 提供类似 Mika 的 Android 手机后台虚拟屏幕（`VirtualDisplay`）操作能力，同时严格保护对话的 Prompt Cache。

## 架构要点

1. **Prompt Cache 零污染（`always_on=False`）**：
   - 不在系统提示词或 `channel.origin` 注入动态设备状态；
   - 通过 `TOOLS.declare_group(ctx, always_on=False)` 注册，模型仅在需要操作手机时按需调用 `load_tools(plugin="phone-use@local")`（或 `phone-use@github`）获取 `phone_devices`、`phone_observe`、`phone_action` 的 schema。
2. **后台虚拟屏与悬浮画中画小窗（`akashic-android-shell` + 无线调试 UID 2000 `ShellService`）**：
   - 手机端通过无线调试配对拉起 UID 2000 `ShellService`，创建带 `FLAG_TRUSTED | FLAG_OWN_DISPLAY_GROUP | FLAG_ALWAYS_UNLOCKED | FLAG_OWN_FOCUS | FLAG_STEAL_TOP_FOCUS_DISABLED` 与 `DISPLAY_IME_POLICY_LOCAL` 的后台虚拟屏；
   - 画面输出到 `ImageReader`，手机端原生悬浮小窗（`AgentPip`）显示预览；焦点隔离依赖设备与 Android 版本，以端侧状态和实测为准。
3. **用户授权与明确接管（Ask / Takeover）**：
   - 在用户授权范围内操作，不因为页面包含密码、验证码或支付文字而强制暂停。只有明确的 `takeover_reason` 才阻止普通动作，保留 `hand_to_user` / `resume_from_user` 协议；省略接管字段不会自动解除已确认接管。
   - 设备报告 `secure_window` 时如实提示 `FLAG_SECURE`、截图可能受限，不将其当作接管指令。Android 的截图、输入和认证权限仍然有效，工具不绕过这些限制，也不保证特定应用或交易可执行。
4. **Turn 闭合自动收尾**：
   - 跟随 `MESSAGE_CATALOG` 与 `TURN_PROJECTION`，当源 Turn 闭合时自动向手机端发送 `finish` 释放虚拟屏、恢复静音与剪贴板，并将 `OWNER_STATE` 的 `phone-use:*` 记录收敛至 `ended`。

## 验证命令

```sh
# 1. 200 seeds Monkey 与真实本地 WebSocket 故障注入（不代表 Android 真机验收）
PYTHONPATH=/home/huashen/coding/akashic-agent pytest -q tests/test_phone_monkey.py tests/test_rpc_delivery.py

# 2. 历史 Core 脚本（脚本模型与模拟节点，不是真实模型或真机验收）
# 依赖旧 agent.plugins.bundles API，需适配目标 Core 后才能运行。
PYTHONPATH=/home/huashen/coding/akashic-agent python3 e2e_scenario.py
```
