# Doubao ASR Bridge for OpenLess

Windows 上的实验性豆包网页语音识别兼容层：独立登录、边录边传、批量备用，以及与 OpenLess 的启停联动。无需让 Murmur 常驻，不注册全局快捷键，不访问麦克风，不负责文字回填。

> 非豆包或 OpenLess 官方项目。它复用登录用户能使用的豆包网页 ASR 协议，不是火山引擎付费 API，也不承诺与 Seed-ASR 2.0 相同的模型、准确率、额度或服务保障。请遵守服务条款，不要用于批量滥用；网页接口变化可能使其失效。

## 为什么单独建仓库

本项目是外置协议适配工具，不修改 OpenLess 官方二进制。它涉及独立的网页登录状态、上游非公开协议和 Windows 生命周期管理，先独立发布更容易维护、回滚和说明支持范围。将来若 OpenLess 维护者愿意接纳，可讨论通用外部 ASR / sidecar 支持，再提交聚焦的上游 PR。

## 工作方式

```text
OpenLess 录音 → 本机 WebSocket → 豆包网页 ASR
             ← 中间识别/最终文本 ←
停止录音 → OpenLess 自己的润色模型 → 回填目标应用
```

- **流式（默认推荐）**：复用 OpenLess 的 Qwen Realtime 协议客户端，每收到音频块就向豆包发送。Qwen 只是客户端协议，不使用阿里云服务。
- **批量（备用）**：兼容 `/v1/audio/transcriptions`；整段上传后分段识别、按顺序拼接。OpenLess 内可选两个独立通道。
- **独立登录**：临时 WebView2 登录窗口；会话由 Windows 当前用户 DPAPI 加密保存。
- **生命周期**：启动器先检查本机服务，再启动 OpenLess；OpenLess 进程退出后停止服务和管理器。关闭主窗口但仍留在托盘不算退出。

## 环境与安装

Windows 10/11、Python **3.11**、Edge WebView2 Runtime、.NET Framework 4.x C# 编译器，以及已经安装的 OpenLess。验证客户端版本为 **2.0.0-Beta.1**，其他版本尚未验证。当前依赖 `audioop`，不要使用 Python 3.13+。

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

只绑定 `127.0.0.1:18765`，拒绝带 Origin 的浏览器请求；**本机接口无强认证**，同一机器其他本地程序仍可能调用它。不要将端口暴露到局域网/公网。音频会上传豆包，不是离线识别。

`data/session.dpapi`、WebView2 profile、备份、日志和录音绝不能上传仓库；DPAPI 保护文件不等于保护同用户恶意进程。代码不记录 Cookies 或上游带凭据 URL。默认仅用独立登录；旧测试兼容选项 `DOUBAO_BRIDGE_ALLOW_MURMUR=1` 会显式读取当前用户 Murmur 配置，不建议日常开启。

## 开发

```powershell
py -3.11 -m venv runtime
.\runtime\Scripts\python.exe -m pip install -r requirements.txt
.\runtime\Scripts\python.exe test_stream_protocol.py
```

协议测试不需要登录或网络，覆盖最后短帧、顺序、中间与最终事件、并发拒绝、Origin 拒绝、取消后的清理。C# 编译命令见 `install.ps1`。不提交编译二进制或个人测试音频。

## 来源与许可

豆包协议与登录思路来自 [lilong7676/doubao-murmur](https://github.com/lilong7676/doubao-murmur)，保留其 MIT 许可 [LICENSE-Murmur.txt](LICENSE-Murmur.txt)。适配器、启动管理与测试在本仓库维护，采用 [MIT](LICENSE) 许可。
