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
   调用 `tool_call(name="phone_devices", arguments={})` 查看当前连入的手机与**已装应用清单**：
   - 如果 `0 devices online`，直接如实告诉用户手机端 `Akashic Shell` 当前未连接或未开启后台控制，不要盲目重试。
   - 只有 1 台手机在线时，后续调用可以省略 `device_id`；多台同时在线必须显式传入。
   - 清单里的显示名就是 `launch_app` 的 `app` 参数，不需要知道包名。用户说的应用不在清单里，就如实说没装，不要猜包名。
3. **启动或观察目标应用**：
   - 后台虚拟屏懒创建：首次 `launch_app` 或 `phone_observe` 时自动建屏，纯聊天回合不建屏。
   - 每次 `phone_action` 都会等界面稳定后自动返回最新快照，**不需要**在动作后再调 `phone_observe`，也不需要插 `wait`。

## 2. 核心设计机制与操作准则

### 后台虚拟屏与主屏隔离
- **焦点隔离**：端侧请求 `FLAG_TRUSTED | FLAG_OWN_DISPLAY_GROUP | FLAG_ALWAYS_UNLOCKED | FLAG_OWN_FOCUS | FLAG_STEAL_TOP_FOCUS_DISABLED` 与 `DISPLAY_IME_POLICY_LOCAL`；这些配置不替代对当前设备焦点行为的验证。
- **借用主屏后台任务**：目标应用已在主屏最近任务里但没显示时，Shell 会把该任务借到后台屏继续用（返回"已把主屏后台的 … 借到后台显示器"），收尾时归还主屏并恢复用户原来的前台，不需要移交用户。
- **主屏正在用（`PHONE_APP_IN_USE`）**：目标应用正显示在主屏（用户可能在用）时不抢。告诉用户稍后再试，或用户同意后 `hand_to_user`；不要反复重试同一次启动。
- **音频状态**：后台虚拟屏不等于独立音频环境。当前 Shell 没有自动静音实现；旧端侧还可能固定报告 `audio_muted=true`，不能据此断言用户听不到声音或音频焦点未受影响。
- **输入结果**：`type_text` 设置输入框全文，会覆盖原有内容；替换文本时无需先调用 `clear_text`。追加内容需先读出现有全文再组成目标文本；只需清空时使用 `clear_text`。操作后检查返回的字段内容；自定义编辑器可能不支持输入。当前 Shell 没有保存并恢复原剪贴板的实现，不承诺静默粘贴或原剪贴板不变。输入失败或结果不明时，先观察，不直接重复输入，也不自行切换到主屏。

### 读快照，不读图（最便宜的足够观察）
快照是紧凑的元素树，一行一个控件，按层级缩进，例如：

```
pixel-9 后台屏 #3 · com.eg.android.AlipayGphone · MainActivity
界面元素（target 可写 "@编号"、可见文字或 "id=资源名"；@编号跨快照有效）：
- textbox "搜索" @4 [id=search_input]
- list @7 [下方还有]
  - item "转账 · 免费转账到支付宝或银行卡" @9
  - item "余额宝 · 昨日收益 0.52" @10
- tab "我的" @15 [当前选中]
```

- 可点击控件已把它里面的文字并入名称（如 `"转账 · 免费转账…"`），直接点这一行即可，不要去找里面的文字节点。
- **定位用 `target`**：`"@9"`、可见文字 `"转账"`（子串、忽略大小写）、两端加引号做精确匹配、或 `"id=search_input"`。同一控件的 `@编号` 在后续快照里保持不变，可以直接沿用；控件消失时会报 `PHONE_NODE_STALE`，按最新快照重选。文字匹配到多个会列出候选，改用 `@编号`。
- `[下方还有]` 表示该列表还能往下滚：`phone_action(action="scroll", target="@7", direction="down")` 只滚这个列表（滚轮选择器、横向标签也这样滚），不写 target 则整屏滑动。
- **截图默认不附**。界面读不到控件文字（自绘、游戏、地图、部分网页或屏蔽无障碍的应用）或动作后界面没有变化时，会自动附上截图并写明原因。只有确实要看颜色、图片、图表等视觉信息时才传 `include_screenshot=true`；只在有截图时才用 `x, y` 坐标点击。

### 合并多步（`steps`）与等待（`wait_for`）
- 下一步不依赖上一步结果时，合并成一次调用，像人一样连续操作：
  ```json
  {"description": "打开支付宝搜索转账", "steps": [
    {"action": "launch_app", "app": "支付宝"},
    {"action": "tap", "target": "搜索"},
    {"action": "type_text", "target": "搜索", "text": "转账"}
  ]}
  ```
  任一步失败就停下，返回失败原因和当时的界面；只返回最后一步的快照。要先看结果才能决定的步骤不要合并。
- 页面加载慢时给那一步加 `wait_for`（一段会出现的文字），出现即返回，最多等约 8 秒；不要用 `wait` 盲等。

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

## 3. 低功耗事件订阅与按需查询（`phone_events` / `device_subscribe`）
- **五类系统回调适配器（0 轮询）**：
  - `notification`：`NotificationListenerService` 实时捕获，支持单条规则设 `auto_cancel: true` 读完自动清除通知栏卡片。
  - `foreground`：UID 2000 绑定 `ITaskStackListener` + `IProcessObserver` 实时切入切出记录（附 `UsageStatsManager` 游标断点补齐）。
  - `appops`：`AppOpsManager.startWatchingActive` 监听定位（`FINE_LOCATION`）、相机（`CAMERA`）、麦克风（`RECORD_AUDIO`）激活与结束。
  - `media`：`MediaSessionManager` 监听媒体播放状态与曲目/章节元数据（无需开通知权限）。
  - `device`：系统广播与网络回调（解锁、息屏、充放电、Wi-Fi SSID/BSSID 切换）。
- **省电机制**：高价值事件（`delivery: "realtime"`）防抖直传；常规流（`delivery: "batch"`）写端侧 SQLite Outbox，等解锁、切 Wi-Fi 或周期窗口打包上传，不抢 WakeLock。
- **动态管理与查询**：
  - `device_subscribe`：随时查看、新增/修改（支持 `exclude_packages` 黑名单、`match.title_regex`/`text_regex` 过滤和 `ttl_seconds` 定时过期）、启停或删除订阅规则，改完立即实时推送给在线手机。
  - `phone_events`：按需查询最近事件、应用前台会话时长或当前已知状态（同时注册进 `userinformation` 并按规则接入 `watch` 事件流）。
