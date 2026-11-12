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
   - 跟随 `MESSAGE_CATALOG` 与 `TURN_PROJECTION`，当源 Turn 闭合时自动向手机端发送 `finish` 请求释放虚拟屏，并将 `OWNER_STATE` 的 `phone-use:*` 记录收敛至 `ended`。
   - 工具不支持按包保留/关闭；`keep_packages` / `close_packages` 会明确报参数错误。收尾不保证应用进程、最近任务或音频已停止。当前 Shell 没有自动静音或保存并恢复原剪贴板的实现，不应向模型承诺这些能力。

## 配套版本与协议

phone-use 0.3.x 需要 Akashic Shell 0.5.0 或更新版本，两端必须一起升级；`/api/phone/apk` 下发仓库内的 `akashic-shell-v0.5.1.apk`（流量下也能重新拉起后台助手）。

- **连接身份**：注册时服务端在 `welcome` 里下发 `connection_id`，端侧的 ACK、结果和状态回传都必须带上它；重连后旧连接的迟到回传返回 409，不会覆盖新状态。
- **快照引用**：每次观察或动作返回 `snapshot_ref`。节点点击、`type_text`、`clear_text` 必须原样带回所属快照的引用，页面或连接变化后旧节点会被拒绝（`PHONE_SNAPSHOT_STALE`），需要重新观察。坐标点击不需要引用。
- **结果不明不重放**：发送后断连或超时会报告"结果不确定，可能已执行"，不自动重试；模型应先 `phone_observe` 核实。
- **收尾归属**：设备记录最近使用它的 Turn。旧 Turn 闭合时如果设备已被新 Turn 使用，就不发送 `finish`，避免释放新 Turn 正在用的屏幕。

## 验证命令

```sh
PYTHONPATH=/path/to/akashic-agent pytest -q tests/test_phone_monkey.py
```
