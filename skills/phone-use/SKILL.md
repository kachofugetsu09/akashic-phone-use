---
name: phone-use
description: 连接并在后台虚拟屏幕（VirtualDisplay）中操作用户的 Android 手机应用。当用户在手机端或桌面端要求操作手机上的 App（如微信、小红书、美团、闹钟、系统设置等）、读取手机屏幕内容、或提到"phoneuse""操作我的手机""在手机上打开"时使用。
---

# Phone Use（Android 端侧后台虚拟屏控制）

`phone-use` 插件通过 `akashic-android-shell` 的无线调试助手（UID 2000 `ShellService`）在**后台虚拟屏幕（`VirtualDisplay`）**中启动并操作端侧支持的 Android 应用，使用悬浮小窗（`AgentPip`）预览。应用兼容性、权限和焦点隔离依赖设备与 Android 版本，以返回状态和实际观察为准。

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
   - 每次 `phone_action` 执行完毕都会自动返回动作后的最新虚拟屏 UI 节点树快照（含 `snapshot_ref` 及每个可交互控件的 `[node_id]`、文本、类名、`bounds` 与 `center` 坐标），无需在每次点击后再单独调一次 `phone_observe`。

## 2. 核心设计机制与操作准则

### 后台虚拟屏与主屏隔离
- **焦点隔离**：端侧请求 `FLAG_TRUSTED | FLAG_OWN_DISPLAY_GROUP | FLAG_ALWAYS_UNLOCKED | FLAG_OWN_FOCUS | FLAG_STEAL_TOP_FOCUS_DISABLED` 与 `DISPLAY_IME_POLICY_LOCAL`；这些配置不替代对当前设备焦点行为的验证。
- **主屏占用避让（`START_IN_USE_ON_MAIN`）**：如果用户此时正在主屏幕前台使用某个 App（非 `Akashic Shell`），端侧拒绝把该 App 强行抽到后台虚拟屏。收到占用提示时，礼貌询问用户或等用户切回聊天界面后再操作。
- **音频状态**：后台虚拟屏不等于独立音频环境。当前 Shell 没有自动静音实现；旧端侧还可能固定报告 `audio_muted=true`，不能据此断言用户听不到声音或音频焦点未受影响。
- **输入结果**：`type_text` 设置输入框全文，会覆盖原有内容；替换文本时无需先调用 `clear_text`。追加内容需先读出现有全文再组成目标文本；只需清空时使用 `clear_text`。操作后检查返回的字段内容；自定义编辑器可能不支持输入。当前 Shell 没有保存并恢复原剪贴板的实现，不承诺静默粘贴或原剪贴板不变。输入失败或结果不明时，先观察，不直接重复输入，也不自行切换到主屏。

### 优先使用结构化 UI 树，按需索取截图
- 默认返回的 `UI Elements` 列表已包含所有可见可交互控件的 `[node_id]`：
  - 点击已知控件优先传 `node_id`：`phone_action(description="点击搜索按钮", action="tap", node_id=3, snapshot_ref="req-...工具返回的完整引用...")`；
  - 只有在自定义绘制界面（游戏、Canvas、无标签图标）导致节点树缺失时，才将 `include_screenshot=true` 传给 `phone_observe` 或 `phone_action`，结合视觉截图用 `x, y` 坐标点击。

### 快照引用（Shell 0.5.0 / phone-use 0.3.0 起）
- 节点点击以及 `type_text` / `clear_text` 必须把目标快照的 `snapshot_ref` 原样传回；文字操作省略 `node_id` 时也必填。
- 每次观察或动作返回新快照后，使用新引用；短 `node_id` 只在所属快照内有效，不能与另一快照的引用混用。
- 缺少引用不会自动使用最新快照。收到 `PHONE_SNAPSHOT_STALE` 时先重新观察，不能用旧编号直接重试。
- 坐标点击不使用节点引用；快照引用不保证页面不会异步变化，仍需检查动作结果。

### 断连与结果丢失
- 明确报告“排队超时，未发送到端侧”表示本次请求没有执行；先处理占用设备的在途操作，再决定是否重新发起。
- 每次请求只发送一次。未收到确认、连接中断或等待结果超时都可能发生在端侧已经执行之后；工具会报告“结果不确定，可能已执行”，不会自动重发。
- 遇到结果不确定时，先用 `phone_devices` 确认设备重连，再显式调用 `phone_observe` 核实状态；无法确认的动作应如实说明，不把超时当作未执行，也不要直接重复点击、输入或收尾。
- `phone_observe` 也可能初始化、唤醒或重建虚拟屏，因此同样不自动重试。端侧明确返回拒绝原因时，按该原因恢复。

### 用户授权与明确接管（Ask / Takeover）
- **按用户授权操作**：密码、验证码或支付字样本身不触发暂停或强制移交。不要因这些内容自行缩减用户已经明确授予的数据读取与输入权限；工具执行结果仍需观察核实。
- **如实报告能力限制**：设备报告 `FLAG_SECURE` 时，截图可能受限；Android 的认证、截图与输入权限仍然适用。不要伪造不可读取的内容或把能力受限说成用户已请求接管，也不保证特定应用或金融交易可执行。
- **明确接管状态**：端侧明确上报 `takeover_reason` 时，普通动作暂停，快照标注 `TAKEOVER REQUIRED`。可以观察状态；观察快照省略该字段不会自动结束接管，恢复需要端侧明确清除状态。
- **主动移交主屏（`hand_to_user`）**：用户要求移交，或任务确实需要用户手动处理时，调用：
  ```json
  {
    "description": "按用户要求移交主屏检查选项",
    "action": "hand_to_user",
    "reason": "请用户检查当前选项"
  }
  ```
  根据端侧返回结果告知用户当前移交状态，请用户完成后回复；不要把请求已发送等同于主屏切换已成功。
- **收回后台继续（`resume_from_user`）**：用户确认手动处理完成后，调用 `phone_action(description="收回后台虚拟屏继续", action="resume_from_user")`，检查接管状态已解除后继续。

### Turn 结束自动收尾
- 当前对话 Turn 闭合（`complete` / `quiet` / `abandoned`）时，插件的后台 follower 会向手机端发送 `finish` 请求释放虚拟屏幕。
- 自动收尾只释放整个后台虚拟屏。工具不支持 `keep_packages` / `close_packages`，传入会明确报参数错误。收尾不保证应用已保留、关闭、停止发声或从最近任务消失，也不包含恢复静音或原剪贴板。

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
