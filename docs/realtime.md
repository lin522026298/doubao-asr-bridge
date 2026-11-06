# OpenAI Realtime 语音直出（0.2.0）

本模块用于普通语音输入：`OpenLess 录音 → 本机兼容层 → GPT-Realtime-2.1 → 整理正文 → OpenLess 插入`。上游只生成文字，思考使用 low。使用官方 API Key 和相应 API 额度，不使用 Codex OAuth 调用音频模型。

## 日常操作

使用已有 OpenLess 快捷方式和录音键即可。兼容层没有单独的日常控制界面，不占用新快捷键，不开启麦克风，不负责插入。退出 OpenLess 托盘进程后，现有启动器会关闭整个兼容服务；仅关闭主窗口、仍在托盘时继续运行。启用的 OpenLess 登录自启动同样经过原启动器，没有第二个独立自启动项。

## 首次配置

先按仓库 README 完成兼容服务安装，再退出 OpenLess。使用本机的掩码输入框保存 API Key，然后启用：

```powershell
.\runtime\Scripts\python.exe .\configure_realtime.py key
.\runtime\Scripts\python.exe .\configure_realtime.py enable
```

密钥保存在本安装目录 `data/openai.dpapi`，仅当前 Windows 用户可解密。不要把密钥放入命令行、仓库或 OpenLess 本机通道的 Key 字段。该字段仅使用 `local-bridge` 占位符。

配置工具新增 ASR 通道 `OpenAI Realtime · 语音直出`，地址 `ws://127.0.0.1:18765/v1/realtime/openai`，借用 `bailian-qwen3-realtime` 的客户端协议。它不调用阿里云。原来的豆包流式、批量通道及文字模型均保留。

同时新增并选择 `Realtime · 清晰结构` 风格。OpenLess 仍从普通流式入口收音，Realtime 已完成整理，因此普通听写不再调用一次独立润色模型。选区编辑和问答保留原来的文字模型；专用风格复制原选区/语音编辑提示词。

## 直出风格的兼容约束

已核对 OpenLess **2.0.1**：只有 `raw` 模式且提示词**精确等于**默认 Raw 提示词时，普通听写才真正直通；自定义 Raw 提示词仍会触发 LLM。配置器校验已验证默认提示词的 SHA-256，不靠名称猜测。

Realtime 的实际整理指令来自兼容层的 `realtime-prompt.txt`，每次请求重新读取。不要把这些指令填入专用风格的 OpenLess Raw 提示词，否则会再次触发独立润色。升级 OpenLess 后应重新检查这一兼容条件；配置器会拒绝无法确认的默认提示词。已有用户自定义 Raw 提示词时，可用 `--raw-prompt-file` 提供当前支持版本经源码核对的默认内容，不覆盖原风格。

## 词典、上下文和边界

- 每次录音读取当前启用词典，最多 256 条，仅用于还原本次实际说到的词。
- 按 OpenLess 历史时间窗口，最多参考最近两轮成功、同直出风格的历史文本；时间窗口为 0 时不附带历史。历史只用于辨认，不作为本次要输出的正文。
- 外部 ASR 协议暂不传递 OpenLess 的 Windows 光标上下文。普通 Realtime 听写不能使用那段周围文字；选区助手仍使用 OpenLess 的原流程。
- 录音期间只上传音频，结束后才生成正文；没有额外调用一个 ASR 来显示录音期间的逐字预览。
- 输入为 16 kHz、单声道 PCM16；兼容层连续转为官方接口的 24 kHz PCM。单次上限五分钟。OpenLess 当前流式客户端的录后等待上限约 12 秒，兼容层在 10 秒内等不到完整结果就明确失败。
- 不启用自动回答，不把录音中的提问、搜索或操作命令当成要执行的任务。不增加第二遍润色。长音频、中英术语和改口仍需实际验证。

## 切回豆包与恢复

在 OpenLess 设置中，把语音识别切回原豆包通道，再把风格切回原 `清晰结构`。只切 ASR、仍保留直出风格时，豆包结果也会直接插入，不再经过原润色步骤。

配置器会先把原偏好、风格和凭据快照加密到 `data/backups/realtime-*/profile.dpapi`。完整恢复需先退出 OpenLess，再运行：

```powershell
.\runtime\Scripts\python.exe .\configure_realtime.py restore --snapshot '快照文件的本机路径'
```

无需单独启停服务。恢复后 API Key 仍留在本机加密文件中，方便以后重新选择使用。豆包登录过期检查仍在实际调用豆包时生效；Realtime 为主用时，启动不因无关豆包登录而弹窗。

## 错误与记录

密钥、认证/权限、额度/速率、网络、超时及不完整输出均通过明确失败事件返回。即使已经收到部分文字，也不把失败伪装成成功最终结果。取消会释放锁和上游连接，允许下一次录音。

`GET /health` 提供版本、密钥存在/可解密状态以及最后一次非敏感调用指标。密钥“已配置”不等于已获服务端认证；实际成功识别才验证相应调用。轮转的 `data/realtime-diagnostics.jsonl` 只记录计数、耗时和错误类型，不保存音频、文字或凭据。

## 参考

- [OpenAI Realtime conversations](https://developers.openai.com/api/docs/guides/realtime-conversations)
- [OpenAI Realtime WebSocket](https://developers.openai.com/api/docs/guides/voice-websockets?api=realtime)
- [GPT-Realtime-2.1](https://developers.openai.com/api/docs/models/gpt-realtime-2.1)
