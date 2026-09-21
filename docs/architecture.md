# 协议与生命周期

## Streaming

`GET /v1/realtime` 接收 `session.update`，随后 `input_audio_buffer.append`（base64 PCM16）和 `session.finish`。响应 `session.updated`、`conversation.item.input_audio_transcription.text`、一次完整的 `conversation.item.input_audio_transcription.completed`，最后 `session.finished`。

豆包每次 Text 被视为累计全文，中间结果替换而非拼接。音频 FIFO 排空后才发送尾部静音，避免松开按键时丢弃队列最后一块。错误发送 transcription.failed，而不是让 OpenLess 将已有 partial 当作成功最终结果。队列上限 100 块，结束等待不超过客户端 12 秒预算。

## Batch

`POST /v1/audio/transcriptions` 接收 multipart WAV。按照低能量位置分割约 16–25 秒音频，最多三个请求并行，按原顺序合并；每一音频字节保留一次。无重试、无摘要去重。实际识别仍可能遗漏。两种入口共享单请求锁。

## Lifetime

C# 启动器采用当前登录会话命名互斥量，避免重复生命周期管理者。它启动 Python 服务，等待 health，然后启动/接管官方 OpenLess。每 500ms 检查目标安装路径的 OpenLess；退出后停止本安装 PID 对应的进程树。Python 服务另监测启动器 PID，避免启动器崩溃后遗留服务。

启动器只重写已经存在且精确指向官方程序路径的 HKCU Run / OpenLess 值；缺失值不重建。它不是所有进程启动的全局拦截器，直接运行原 exe 无法触发桥接服务。

## 本地数据

`data/` 为运行时专用，不纳入 Git。DPAPI 文件只用于当前 Windows 用户。WebView2 profile 也含登录状态，不能随源码打包。health 只报告会话文件是否存在，不表示认证有效。没有端口令牌鉴权，部署边界是可信的本机环境。

## 已知维护项

一次现场日志只记录 `Service cleanup failed`，原因未被详细保存，不能直接判断是残留还是退出竞争。随后用户重启、自启动、登录及语音输入正常。后续可增加不含敏感数据的退出码诊断，并扩大生命周期自动测试；本版本没有为排查而改动已验收的运行环境。
