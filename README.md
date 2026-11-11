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

## SSE 连接身份与配套版本

`phone-use 0.2.0` 的 Android SSE 通道要求 Akashic Shell **0.4.0 或更新版本**，必须配套升级，不能单边部署。旧版不携带连接身份，迟到的 HTTP 状态会覆盖重连后的新状态。

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
- `/api/phone/apk` 只提供 `akashic-shell-v0.4.0.apk`。兼容的 release 制品缺席时明确返回 HTTP 404，不回退到仓库中的旧版 APK；debug 构建不等同已发布 release。

### 随附 Shell 0.4.0 制品

- 来源：[`akashic-android-shell` PR #9](https://github.com/kachofugetsu09/akashic-android-shell/pull/9)，源码 `0ca40af54ecf2214def92e041a6ee0abbb25c92a`。
- 实际变体：`assembleRelease`，R8 与资源裁切启用；包名 `com.akashic.shell`，versionName `0.4.0`、versionCode `12`。
- APK SHA-256：`86683f2ddae7533eafee325eb0e213951e005c20c433112596c46746e3ee52e0`。
- 遵循源码中的演示签名配置，使用 Android Debug 证书（SHA-256 `c3dfa0211bb6569291eb214046f65c2e5b8e3a226c1dca6da17c39091ef01f58`）。这是 release 变体，不代表已发布 GitHub release 或完成生产部署。

旧随附 APK 的证书 SHA-256 为 `376ea17860ec9b603006f0077035d533b901a36ea13982cb5311d79121b8b8f9`，与本制品不同，Android 会拒绝同包名的直接覆盖升级。下载入口不执行卸载或清除数据；正式替换前需核对实际已安装证书，并单独安排签名兼容或数据保留方案。源码回退不能撤销手机安装效果。

## 验证命令

```sh
# 1. 200 seeds Monkey 与真实本地 WebSocket 故障注入（不代表 Android 真机验收）
PYTHONPATH=/home/huashen/coding/akashic-agent pytest -q tests/test_phone_monkey.py tests/test_rpc_delivery.py tests/test_sse_delivery.py

# 2. 历史 Core 脚本（脚本模型与模拟节点，不是真实模型或真机验收）
# 依赖旧 agent.plugins.bundles API，需适配目标 Core 后才能运行。
PYTHONPATH=/home/huashen/coding/akashic-agent python3 e2e_scenario.py
```
