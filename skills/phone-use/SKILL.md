---
name: phone-use
description: 连接并在后台虚拟屏幕（VirtualDisplay）中操作用户的 Android 手机应用。当用户在手机端或桌面端要求操作手机上的 App（如微信、小红书、美团、闹钟、系统设置等）、读取手机屏幕内容、或提到"phoneuse""操作我的手机""在手机上打开"时使用。
---

# Phone Use（Android 端侧后台虚拟屏控制）

`phone-use` 插件让 Akashic 在不打断用户手机主屏幕（前台聊天与输入焦点保持不变）的前提下，通过 `akashic-android-shell` 的无线调试常驻助手（UID 2000 `ShellService`）在**后台虚拟屏幕（`VirtualDisplay`）**中启动并操作任意 Android 应用，同时在用户手机右下角提供实时画中画悬浮小窗（`AgentPip`）预览。

为了保护对话的 Prompt Cache（前缀缓存），手机在线状态绝不写进系统提示词或 `channel.origin`，`phone-use` 工具组也**不在常驻工具 schema 中**。

## 1. 解锁与标准调用链路

需要操作或检查手机时，严格按以下顺序执行：

1. **按需加载工具组**：
   调用 `load_tools`，传入插件工具目录中 `phone-use` 的准确 ID（如 `{"plugin": "phone-use@local"}`、`{"plugin": "phone-use@github"}` 或 `{"plugin": "phone-use"}`），获取 `phone_devices`、`phone_observe`、`phone_action` 三个工具的完整 schema。
2. **拉取在线手机列表（`phone_devices`）**：
   调用 `tool_call(name="phone_devices", arguments={})` 查看当前连入的手机：
   - 确认 `device_id`（例如 `"pixel-9"`）、`display_mode`（`"virtual_display"` 表示后台独立焦点虚拟屏已就绪）、`own_focus=true` 与 `steal_top_focus_disabled=true`；
   - 如果 `0 devices online`，直接如实告诉用户手机端 `Akashic Shell` 当前未连接或未开启后台控制，不要盲目重试。
   - 如果只有 1 台手机在线，后续 `phone_observe` / `phone_action` 可以省略 `device_id`；若有多台手机同时在线，必须显式传入目标 `device_id`。
3. **启动或观察目标应用（`phone_action` / `phone_observe`）**：
   - 后台虚拟屏采用懒创建（`LazyDisplay`）：首次调用 `phone_action(action="launch_app", package_name="...")` 或 `phone_observe` 时自动建屏，纯聊天回合从头到尾不建屏。
   - 每次 `phone_action` 执行完毕都会自动返回动作后的最新虚拟屏 UI 节点树快照（含每个可交互控件的 `[node_id]`、文本、类名、`bounds` 与 `center` 坐标），无需在每次点击后再单独调一次 `phone_observe`。

## 2. 核心设计机制与操作准则

### 后台虚拟屏与主屏隔离
- **不抢主屏焦点**：虚拟屏带有 `FLAG_TRUSTED | FLAG_OWN_DISPLAY_GROUP | FLAG_ALWAYS_UNLOCKED | FLAG_OWN_FOCUS | FLAG_STEAL_TOP_FOCUS_DISABLED` 以及 `DISPLAY_IME_POLICY_LOCAL`。用户在手机主屏 `Akashic Shell` 里打字聊天时，后台虚拟屏的点击和输入法不会抢走主屏焦点。
- **主屏占用避让（`START_IN_USE_ON_MAIN`）**：如果用户此时正在主屏幕前台使用某个 App（非 `Akashic Shell`），端侧拒绝把该 App 强行抽到后台虚拟屏。收到占用提示时，礼貌询问用户或等用户切回聊天界面后再操作。
- **后台自动静音与静默粘贴**：后台虚拟屏打开音视频应用时自动通过 `appops PLAY_AUDIO ignore` 静音；`type_text` 遇到非标准输入框时通过 `shell` 剪贴板附带 `SUPPRESS_CLIPBOARD_OVERLAY` 与 `EXTRA_IS_SENSITIVE` 粘贴并立即还原用户原剪贴板。

### 优先使用结构化 UI 树，按需索取截图
- 默认返回的 `UI Elements` 列表已包含所有可见可交互控件的 `[node_id]`：
  - 点击已知控件优先传 `node_id`：`phone_action(description="点击搜索按钮", action="tap", node_id=3)`；
  - 只有在自定义绘制界面（游戏、Canvas、无标签图标）导致节点树缺失时，才将 `include_screenshot=true` 传给 `phone_observe` 或 `phone_action`，结合视觉截图用 `x, y` 坐标点击。

### 断连与结果丢失
- 明确报告“排队超时，未发送到端侧”表示本次请求没有执行；先处理占用设备的在途操作，再决定是否重新发起。
- 每次请求只发送一次。未收到确认、连接中断或等待结果超时都可能发生在端侧已经执行之后；工具会报告“结果不确定，可能已执行”，不会自动重发。
- 遇到结果不确定时，先用 `phone_devices` 确认设备重连，再显式调用 `phone_observe` 核实状态；无法确认的动作应如实说明，不把超时当作未执行，也不要直接重复点击、输入或收尾。
- `phone_observe` 也可能初始化、唤醒或重建虚拟屏，因此同样不自动重试。端侧明确返回拒绝原因时，按该原因恢复。

### 敏感动作护栏与主屏接管（Ask / Takeover）
- **绝不代点支付与密码**：当后台虚拟屏进入收银台、支付密码、转账确认、生物识别、`FLAG_SECURE` 安全窗口或短信/滑块验证页面时，端侧会自动把悬浮小窗切换为**橙色呼吸光晕（`pip_state="needs_you"`）**并在快照顶部标注 `TAKEOVER REQUIRED`。
- **主动移交主屏（`hand_to_user`）**：遇到需要用户本人输入密码、确认付款或扫码时，调用：
  ```json
  {
    "description": "移交主屏请用户确认支付",
    "action": "hand_to_user",
    "reason": "请确认订单金额并完成支付验证"
  }
  ```
  然后在对话里告诉用户：“我已经把页面切到手机主屏（或点击右下角橙色呼吸小窗即可打开），请确认完成后告诉我。”并结束当前回合。
- **收回后台继续（`resume_from_user`）**：当用户回复“好了/付完了”后，调用 `phone_action(description="收回后台虚拟屏继续", action="resume_from_user")` 检查完成状态并收尾。

### Turn 结束自动收尾
- 当前对话 Turn 闭合（`complete` / `quiet` / `abandoned`）时，`phone-use` 插件的后台 follower 会自动向手机端发送 `finishDisplay`，释放虚拟屏幕、恢复被静音的应用并还原剪贴板；也可以在最后一步 `phone_action` 中通过 `keep_packages` / `close_packages` 指定哪些应用保留在系统最近任务、哪些直接关闭。

## 3. 常见包名速查

| 应用 | 包名 (`package_name`) |
|---|---|
| 微信 | `com.tencent.mm` |
| 小红书 | `com.xingin.xhs` |
| 美团 | `com.sankuai.meituan` |
| 淘宝 | `com.taobao.taobao` |
| 哔哩哔哩 | `tv.danmaku.bili` |
| Telegram | `org.telegram.messenger` |
| 系统设置 | `com.android.settings` |
| 系统时钟/闹钟 | `com.google.android.deskclock` |
| Chrome 浏览器 | `com.android.chrome` |
