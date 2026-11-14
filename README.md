# OpenLess Voice Bridge（豆包 / OpenAI Realtime）

Windows 上的外置语音兼容层：豆包网页识别支持独立登录、边录边传和批量备用；官方 OpenAI Realtime 支持语音直接生成整理正文。两种通道共用 OpenLess 的启停联动。无需让 Murmur 常驻，不注册全局快捷键，不访问麦克风，不负责文字回填。

**0.3.2 改进口述修订**：明确改口只修改本次录音指向的内容，引用、否定及外部操作请求保留原意。思考档位支持本机配置，默认 `low`；`minimal` 是更低的公开档位，`none` 仅供实验性手动使用。

短句保持简短，长段中的明确并列事项分别换行；最终中文默认用本地 OpenCC 规范为简体，并支持仅供 Realtime 使用的补充词表。没有新增模型调用，豆包保持原有处理流程。实际准确性仍以录音验证为准。

**0.2.0 新增官方 OpenAI Realtime 语音直出**：录音期间上传音频，结束后直接返回整理正文。与豆包共用现有辅助进程；OpenLess 启动即运行，退出即结束。API Key 独立加密保存。安装和两种通道切换见 [Realtime 接入说明](docs/realtime.md)。

**0.3.0 新增可关闭的 Realtime 光标上下文**：按 OpenLess 2.0.1 的 UIA 采集规则读取当前输入框附近最多 600 字符。仅用于 GPT Realtime，尊重 OpenLess 的上下文总开关；读取未及时完成就跳过，不等待额外模型调用。新安装默认关闭，安装目录的 `Realtime光标上下文设置.lnk` 可切换。豆包路由不执行新增读取逻辑。

> 非豆包或 OpenLess 官方项目。它复用登录用户能使用的豆包网页 ASR 协议，不是火山引擎付费 API，也不承诺与 Seed-ASR 2.0 相同的模型、准确率、额度或服务保障。请遵守服务条款，不要用于批量滥用；网页接口变化可能使其失效。

## 为什么单独建仓库

本项目是外置协议适配工具，不修改 OpenLess 官方二进制。它涉及独立的网页登录状态、上游非公开协议和 Windows 生命周期管理，先独立发布更容易维护、回滚和说明支持范围。将来若 OpenLess 维护者愿意接纳，可讨论通用外部 ASR / sidecar 支持，再提交聚焦的上游 PR。

## 工作方式

豆包链路如下；Realtime 直接返回整理正文，跳过普通听写的独立润色，详见[专用说明](docs/realtime.md)。

```text
OpenLess 录音 → 本机 WebSocket → 豆包网页 ASR
             ← 中间识别/最终文本 ←
停止录音 → OpenLess 自己的润色模型 → 回填目标应用
```

- **流式（默认推荐）**：复用 OpenLess 的 Qwen Realtime 协议客户端，每收到音频块就向豆包发送。Qwen 只是客户端协议，不使用阿里云服务。
- **批量（备用）**：兼容 `/v1/audio/transcriptions`；整段上传后分段识别、按顺序拼接。子段失败时保留成功结果并标明缺失区间，默认不把部分结果当作完整成功。详见[批量接口与客户端处理](docs/batch-api.md)。OpenLess 内可选两个独立通道。
- **独立登录**：临时 WebView2 登录窗口；会话由 Windows 当前用户 DPAPI 加密保存。
- **登录失效提示**：启动时检查本地会话；明确过期、缺失或不可读时弹窗。识别中遇到登录相关拒绝也提供重新登录入口，同一会话连续失败不重复弹窗。
- **生命周期**：启动器先检查本机服务，再启动 OpenLess；OpenLess 进程退出后停止服务和管理器。关闭主窗口但仍留在托盘不算退出。

## 环境与安装

Windows 10/11、Python **3.11**、Edge WebView2 Runtime、.NET Framework 4.x C# 编译器，以及已经安装的 OpenLess。已验证客户端版本包括 **2.0.0-Beta.1** 和 **2.0.1**。当前依赖 `audioop`，不要使用 Python 3.13+。

```powershell
git clone https://github.com/lin522026298/doubao-asr-bridge.git
cd doubao-asr-bridge
.\install.ps1
```

安装脚本将白名单源码复制到 `%LOCALAPPDATA%\Programs\DoubaoAsrBridge`，与 `%LOCALAPPDATA%\Programs\OpenLess` 并列，创建虚拟环境、从 PyPI 安装依赖并编译启动器。首次安装使用；已有安装会拒绝覆盖，避免触及现有会话和验收版本。

安装后：

1. 打开安装目录的 `登录豆包.lnk`，自行登录，提示已保存后关闭窗口。保存状态不等于接口认证成功，需试录确认。
2. 退出 OpenLess，运行下面的配置命令。它只新增两个 ASR 通道并选择流式；偏好和凭据先备份到本机 `data/backups`，润色设置保持不变。

```powershell
cd "$env:LOCALAPPDATA\Programs\DoubaoAsrBridge"
.\runtime\Scripts\python.exe .\configure_openless.py
```

3. 使用安装脚本创建的桌面 `OpenLess（豆包联动）.lnk` 启动，然后使用 OpenLess 自己的录音热键。
4. 若需要登录自启动，在通过启动器运行的 OpenLess 中启用该选项。管理器会把 OpenLess 写入的原程序启动命令路由到自身；关闭自启动时不重新创建该项。

### 通道参数

| 参数 | 实时流式 | 批量备用 |
|---|---|---|
| 通道名 | 豆包网页 ASR · 实时流式 | 豆包网页 ASR · 批量（录完后处理） |
| providerType | `bailian-qwen3-realtime` | `openai-compatible` |
| 地址 | `ws://127.0.0.1:18765/v1/realtime` | `http://127.0.0.1:18765/v1` |
| 模型 | `doubao-web-asr` | `doubao-web-asr` |
| API Key | `local-bridge`（本机占位符） | 空 |

在 OpenLess 的“设置 → AI 服务与模型 → 语音识别”切换通道。`local-bridge` 不是豆包密钥，也不提供本机认证。

### 提示登录过期时

弹窗选择“是”会打开独立豆包登录窗口；选择“否”后仍可稍后打开安装目录的 `登录豆包.lnk`。登录信息保存后关闭登录窗口，再试一次录音即可，兼容层会在下一次请求重新加载会话，无需重启 OpenLess。

启动检查使用登录时保存的 Cookie 有效期；旧版会话仅在独立 WebView2 的 Cookie 创建时间与会话保存时间吻合时读取对应有效期元数据。未知有效期保持“已保存，尚未验证”，不推断已登录或已过期。启动检查不上传音频，也不保证尚未到期的登录没有被服务端撤销；撤销或权限拒绝仍需由实际识别请求发现。

`710022013` 曾在登录过期时出现，但没有足够依据断言它只表示过期，因此提示检查登录；HTTP 401/403 提示检查登录或权限。网络断开、超时及其他上游错误不会被直接标记为登录过期。弹窗在当前服务进程内按会话版本去重，重新登录或识别成功后可再次提示；重启服务会重新检查。

`GET /health` 的 `status: ok` 仅表示本机服务正常。新增 `login.status` 区分 `saved_unverified`、`validated`、`login_expired`、`login_missing`、`login_unreadable` 和请求中的登录拒绝；`server_validated` 只有当前会话实际识别成功后才为真，重新登录后复位。

## 验证与限制

- 一台 Windows 机器用户实测重启、自启动、登录保持和语音输入通过；不是广泛兼容性保证。
- 125 秒公开样本模拟实时发送，录音中持续返回中间结果，停止后约 **4.5 秒**完成 ASR；不含后续 LLM 润色。测试流因定时开销实际发送约 132 秒。
- 长样本仍有错字与遗漏；流式降低录后等待，不保证准确率或完整率。不能推导其优于其他识别模型。
- PCM16、16 kHz、单声道；最长五分钟，同时只支持一个识别请求。
- 最终结果使用上游 finish 或两秒结果静默的启发式，不是完整性确认协议。
- **必须通过启动器启动**。直接运行官方 `openless.exe`、旧任务栏固定项或升级重建的快捷方式会绕过联动。
- 一次历史日志出现服务清理失败，但没有详细错误原因；之后重启和语音实测通过。仍需扩大退出/崩溃路径覆盖。
- 无自动恢复上游连接、无刷新登录保证、无自动更新安装器。

## 隐私与安全边界

只绑定 `127.0.0.1:18765`，拒绝带 Origin 的浏览器请求；**本机接口无强认证**，同一机器其他本地程序仍可能调用它。不要将端口暴露到局域网/公网。音频会上传当前选择的豆包或 OpenAI 服务，不是离线识别。Realtime 还会上传用于辨认的词典、所选历史上下文，以及显式开启且采集成功的光标参考。

`data/session.dpapi`、WebView2 profile、备份、日志和录音绝不能上传仓库；DPAPI 保护文件不等于保护同用户恶意进程。代码不记录 Cookies 或上游带凭据 URL。默认仅用独立登录；旧测试兼容选项 `DOUBAO_BRIDGE_ALLOW_MURMUR=1` 会显式读取当前用户 Murmur 配置，不建议日常开启。

## 开发

```powershell
py -3.11 -m venv runtime
.\runtime\Scripts\python.exe -m pip install -r requirements.txt
.\runtime\Scripts\python.exe test_stream_protocol.py
.\runtime\Scripts\python.exe test_login_expiry.py
.\runtime\Scripts\python.exe -m unittest test_batch_asr -v
.\runtime\Scripts\python.exe -m unittest test_openai_realtime -v
.\runtime\Scripts\python.exe -m unittest test_cursor_context -v
```

测试不需要登录或网络，也不弹出真实窗口。协议检查覆盖最后短帧、顺序、中间与最终事件、并发拒绝、Origin 拒绝、取消后的清理；登录回归覆盖过期判断、旧会话保守读取、弹窗去重、网络错误分类及两种识别入口；批量回归覆盖子段隔离、全局偏移、严格/部分结果、静音与非零噪声、超时及无正文日志；Realtime 回归覆盖初始化缓冲、连续重采样、最终结果先回传、失败后半截文本、取消释放，以及配置和凭据保护。C# 编译命令见 `install.ps1`。不提交编译二进制或个人测试音频。

## 来源与许可

豆包协议与登录思路来自 [lilong7676/doubao-murmur](https://github.com/lilong7676/doubao-murmur)，保留其 MIT 许可 [LICENSE-Murmur.txt](LICENSE-Murmur.txt)。原适配器、启动管理与 Python 测试在本仓库维护，采用 [MIT](LICENSE) 许可。

独立的 `cursor_reader/` 组件移植自 OpenLess 2.0.1 的光标采集和参考提示词，单独采用 [AGPL-3.0-only](cursor_reader/LICENSE-AGPL-3.0.txt)，不适用根目录 MIT 声明。来源、修改和限制见 [UPSTREAM.md](cursor_reader/UPSTREAM.md)。源码随仓库提供，由安装脚本在本机编译；它与桥接服务只通过匿名管道交换一次请求的数据，没有网络服务或独立自启动项。
