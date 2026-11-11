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
   - 跟随 `MESSAGE_CATALOG` 与 `TURN_PROJECTION`，当源 Turn 闭合时自动向手机端发送 `finish` 请求释放虚拟屏，并记录 `OWNER_STATE` 的 `phone-use:*` 收尾状态。
   - 工具不支持按包保留/关闭；`keep_packages` / `close_packages` 会明确报参数错误。收尾不保证应用进程、最近任务或音频已停止。当前 Shell 没有自动静音或保存并恢复原剪贴板的实现，不应向模型承诺这些能力。

## SSE 连接身份与配套版本

`phone-use 0.3.0` 的 Android 通道要求 Akashic Shell **0.5.0 或更新版本**，必须配套升级，不能单边部署。旧版不携带连接身份，迟到的 HTTP 状态会覆盖重连后的新状态。

```text
┌──────────────────────┐  POST hello：supports_connection_id=true  ┌──────────────────────┐
│ Android stream owner ├────────────────────────────────────────►│ PhoneBridge 当前连接 │
│ 每条线程捕获所属 ID  │◄──────── welcome：connection_id ────────┤ 每次注册生成新 ID    │
│ ACK / result / state ├── POST reply：携带同一 connection_id ──►│ 只接受当前连接回传   │
└──────────────────────┘                                        └──────────────────────┘
```

- `/api/phone/stream`：hello 必须声明 `supports_connection_id=true`；缺少时返回 HTTP 400 升级提示，不替换现有连接。
- `/api/phone/reply`：ACK、response 和独立 state 都必须携带所属 welcome 的 `connection_id`。缺少、错误或过期身份返回 HTTP 409，不应用状态；合法身份但设备已离线返回 HTTP 404。
- 重连后旧线程继续使用旧身份，不能给旧状态补上新连接 ID。重复 response 在同一有效连接内仍可确认接收，但不能覆盖已完成或其他请求的状态。
- WebSocket 帧继续由所属 session 绑定；其 welcome 也提供 `connection_id`，用于可选的 HTTP 回传。
- `/api/phone/apk` 只提供 `akashic-shell-v0.5.0.apk`。兼容的 release 制品缺席时明确返回 HTTP 404，不回退到仓库中的旧版 APK；debug 构建不等同已发布 release。

仓库配套 APK 的源码提交、SHA-256、包版本与证书指纹见
[`akashic-shell-v0.5.0.provenance.json`](akashic-shell-v0.5.0.provenance.json)。
这是启用 R8 的 `release` 变体，沿用 Shell 仓库当前的 **Android Debug 签名配置**；
不表示已发布正式签名或 GitHub Release。旧的 0.3.4 文件已移除，下载端点不会提供不兼容版本。

## 验证命令

```sh
# 1. 200 seeds Monkey 与真实本地 WebSocket 故障注入（不代表 Android 真机验收）
PYTHONPATH=/home/huashen/coding/akashic-agent pytest -q tests/test_phone_monkey.py tests/test_rpc_delivery.py tests/test_sse_delivery.py

# 2. 历史 Core 脚本（脚本模型与模拟节点，不是真实模型或真机验收）
# 依赖旧 agent.plugins.bundles API，需适配目标 Core 后才能运行。
PYTHONPATH=/home/huashen/coding/akashic-agent python3 e2e_scenario.py
```

## 节点引用是快照的一部分（破坏性协议升级）

最终屏幕返回 `snapshot_ref`（产生该屏幕的 RPC UUID）；`node_id` 仍是简短整数。
点击节点、`type_text` 或 `clear_text` 时原样传回所属快照引用，例如：

```json
{"description":"点击搜索框","action":"tap","node_id":3,"snapshot_ref":"req-0123456789abcdef0123456789abcdef"}
```

每次观察或动作结果都产生新引用。连接或显示器更换、空树及已开始的 RPC 失败也会使旧节点失效。
收到 `PHONE_SNAPSHOT_STALE` 后重新观察；缺少引用明确拒绝，工具不自动补上最新值。
即使不传 `node_id`、向聚焦输入框输入，仍须传 `snapshot_ref`。
坐标点击不使用缓存节点，不要求引用，也不因此获得页面稳定性保证。
新插件在注册前要求 Shell hello 声明 `supports_snapshot_ref=true`；旧 Shell 必须升级。
旧插件会丢弃屏幕引用，新 Shell 会拒绝它的节点操作；两端必须配套升级。

真实 Core 工具链协议回归（临时 workspace、真实注册表/账本/UDS，协议 peer 非真机）：

```sh
AKASHIC_CORE_ROOT=/path/to/core /path/to/python snapshot_ref_scenario.py
```

覆盖 schema 暴露引用、缺少引用不发送、观察和动作结果完整展示引用、动作原样传回引用；
它不验证 Android 的节点归属检查，也不是固定预算模型成功率。

## 并发 Turn 与收尾结果

同一手机由最新提交设备调用的 Turn 拥有收尾权。若 A 使用手机后 B 已提交调用，A 闭合只结束自己的记录，不释放 B 正在用的屏幕。当前 Turn 身份保存在既有 `OWNER_STATE` 的 `phone-device:*` 回执中；claim、owner 检查和发送共用 Bridge 的设备锁，旧 binding 仍打开当前 provider。此合同不隔离不同 Turn 的应用内容；并行对话仍共享同一屏幕。

```text
┌──────────────┐    ┌────────────────────────────────────┐
│ Turn A 闭合  ├───►│ 设备锁：当前 owner 仍是 A？          │
└──────────────┘    ├───────────────┬────────────────────┤
                   │ 是：发送 finish│ 否：只结束 A 的记录 │
                   └───────────────┴────────────────────┘
```

- `cleanup_outcome=confirmed`：端侧已回传成功结果；调用记录为 `ended`。
- `not_sent`：确定未发送；暂时离线的记录保留 `started`，消息或连接变化会重访。若新 Turn 已占用，旧记录直接结束并保留未发送原因。
- `unknown`：可能已执行，调用记录为 `cleanup_unknown`，保留原因，重连或插件换代均不自动重放。先观察设备，再按用户当前意图显式恢复。发送前先将设备回执记为未知，以覆盖取消或进程退出窗口。
- 设备回执 IO 也消耗原调用期限；过期不发送。Core 取消磁盘事务时会等待实际磁盘工作结束，不能把取消当作已回滚。

真实 Core 与本地 UDS WebSocket 回归（故障节点 fixture，不代表 Android 真机验收）：

```sh
AKASHIC_CORE_ROOT=/path/to/akashic-agent python3 turn_owner_scenario.py
```

## 当前应用身份的未知值

`PhoneScreenSnapshot.package_name` / `activity_name` 保持字符串；端侧缺失、null 或空串
都解码为空串，工具展示 `unknown`，不会补造 `com.android.shell/MainActivity`。
包名已知但 Activity 未知时分别显示。Shell 也需配套停止使用跨屏历史事件和默认包名；
新插件无法辨认旧 Shell 已经填入的假名称。该字段是观察结果，不是动作的目标应用参数。

## 收尾只释放整块后台显示器

agent 没有 public finish 动作；Turn 闭合后自动发出的 finish 只释放整个 VD。
新调用不接受、转发或记录 keep/close 包选择。旧 v1 owner 回执中的列表保持原样，
只在既有回执读取边界忽略这些无实现的选择，不妨碍自动 release，也不改写历史列表。
端侧兼容接收旧 finish payload，但 kept/closed 只返回实际空数组，不把请求回显当成效果。
最近任务、进程和音频是否停止仍不受保证。
