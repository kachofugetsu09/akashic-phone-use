# phone-use (Akashic Android 端侧后台虚拟屏控制插件)

独立外置插件仓库（支持 `plugin-install` 热安装与热重载），为 Akashic Agent 提供类似 Mika 的 Android 手机后台虚拟屏幕（`VirtualDisplay`）操作能力，同时严格保护对话的 Prompt Cache。

## 架构要点

1. **Prompt Cache 零污染（`always_on=False`）**：
   - 不在系统提示词或 `channel.origin` 注入动态设备状态；
   - 通过 `TOOLS.declare_group(ctx, always_on=False)` 注册，模型仅在需要操作手机时按需调用 `load_tools(plugin="phone-use@local")`（或 `phone-use@github`）获取 `phone_devices`、`phone_observe`、`phone_action` 的 schema。
2. **后台虚拟屏与悬浮画中画小窗（对齐 `akashic-android-shell` + 无线调试 UID 2000 `ShellService`）**：
   - 手机端通过无线调试配对拉起 UID 2000 `ShellService`，创建带 `FLAG_TRUSTED | FLAG_OWN_DISPLAY_GROUP | FLAG_ALWAYS_UNLOCKED | FLAG_OWN_FOCUS | FLAG_STEAL_TOP_FOCUS_DISABLED` 与 `DISPLAY_IME_POLICY_LOCAL` 的后台虚拟屏；
   - 画面输出到 `ImageReader`，手机端右下角原生悬浮小窗（`AgentPip`）实时显示预览，不抢用户主屏幕聊天与打字焦点。
3. **敏感动作护栏与主屏接管（Ask / Takeover）**：
   - 检测到支付收银台、支付密码、转账确认、`FLAG_SECURE` 窗口或验证码时，自动切换为 `pip_state="needs_you"`（橙色呼吸光晕），挂起后台点击并引导用户在主屏幕完成操作后再 `resume_from_user`。
4. **Turn 闭合自动收尾**：
   - 跟随 `MESSAGE_CATALOG` 与 `TURN_PROJECTION`，当源 Turn 闭合时自动向手机端发送 `finish` 释放虚拟屏、恢复静音与剪贴板，并将 `OWNER_STATE` 的 `phone-use:*` 记录收敛至 `ended`。

## 验证命令

```sh
# 1. 200 seeds 确定性 Monkey 测试（覆盖多设备路由、并发断连、收银台接管护栏、主屏冲突）
PYTHONPATH=/home/huashen/coding/akashic-agent pytest -q tests/test_phone_monkey.py

# 2. 真实 Agent 全链路 E2E 验收（验证 Prompt Cache 哈希不变 + 完整 7 步工具调用 + Turn 自动收尾）
PYTHONPATH=/home/huashen/coding/akashic-agent python3 e2e_scenario.py
```
