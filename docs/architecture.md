# 协议与生命周期

## Streaming

`GET /v1/realtime` 接收 `session.update`，随后 `input_audio_buffer.append`（base64 PCM16）和 `session.finish`。响应 `session.updated`、`conversation.item.input_audio_transcription.text`、一次完整的 `conversation.item.input_audio_transcription.completed`，最后 `session.finished`。

豆包每次 Text 被视为累计全文，中间结果替换而非拼接。音频 FIFO 排空后才发送尾部静音，避免松开按键时丢弃队列最后一块。错误发送 transcription.failed，而不是让 OpenLess 将已有 partial 当作成功最终结果。队列上限 100 块，结束等待不超过客户端 12 秒预算。

## Batch

`POST /v1/audio/transcriptions` 接收 multipart WAV。按照低能量位置分割约 16–25 秒音频，最多三个请求并行，按原顺序合并；每一音频字节保留一次。无重试、无摘要去重。实际识别仍可能遗漏。两种入口共享单请求锁。

`batch_asr.py` 按子段隔离结果与异常；单段失败不取消其余任务。子段和总截止都生成明确错误；总截止保留已完成结果，调用者取消才整体清理任务。`asr_errors.py` 对无转写、提前关闭和超时保留数值协议统计；上游空 Text 不等于登录失败或已证实没有语音。只跳过严格全零 PCM，非零低音量/噪声仍交给上游，失败范围不能静默删除。

返回 `segment_results`、`complete`、`partial` 与全局 `missing_ranges`；默认不完整即 HTTP 502，显式允许部分结果的客户端可取得 200，但 `complete` 仍为 false。全部无文本为 422。详细字段、恢复和兼容边界见[批量接口](batch-api.md)。不完整批次的日志仅记录数值与状态白名单，按约 1 MB、一个备份文件轮转，无音频或转写正文。

## Lifetime

C# 启动器采用当前登录会话命名互斥量，避免重复生命周期管理者。它启动 Python 服务，等待 health，然后启动/接管官方 OpenLess。每 500ms 检查目标安装路径的 OpenLess；退出后停止本安装 PID 对应的进程树。Python 服务另监测启动器 PID，避免启动器崩溃后遗留服务。

启动器只重写已经存在且精确指向官方程序路径的 HKCU Run / OpenLess 值；缺失值不重建。它不是所有进程启动的全局拦截器，直接运行原 exe 无法触发桥接服务。

## 本地数据

`data/` 为运行时专用，不纳入 Git。DPAPI 文件只用于当前 Windows 用户。WebView2 profile 也含登录状态，不能随源码打包。没有端口令牌鉴权，部署边界是可信的本机环境。

## 登录状态与通知

`login.py` 在已有的 DPAPI 会话中增加 `cookie_expiries`（Unix 秒）与 `saved_at`。`login_state.py` 仅在所有现存认证 Cookie 的有效期均已知且均已到期时判断为过期；任一认证 Cookie 仍可能有效时保留未验证状态。旧格式不强制迁移或覆盖，最多只读独立 WebView2 Cookie 数据库中的名称、创建时间和有效期。创建时间必须在对应会话保存前 60 秒到保存后 5 秒内，所有认证 Cookie 均可关联才采用，其他情况保持未知。数据库连接显式关闭，不留占用。

服务启动的本地检查不依赖网络，不阻塞 OpenLess 启动。health 的 `status` 保留服务健康含义，`login` 单独报告会话状态。实际识别成功才设置当前会话的 `server_validated`；凭据文件版本改变后复位，元数据到期优先于旧的验证结果。

识别入口保留经过限定的错误类型和数字上游码，不把带凭据的网络异常文本回传。`710022013` 属于需要检查登录的线索，401/403 属于认证或权限被拒绝，都不宣称精确证明过期；普通网络故障不触发登录弹窗。实时客户端仍收到 `transcription.failed`，不会把失败前的 partial 当作最终成功。

`login_notice.py` 以服务的子进程按需显示 Windows 对话框，事件循环不等待用户。用户选择“是”才打开原有独立登录窗口，不自动登录或刷新会话。当前服务按凭据文件版本去重，已有提示尚未关闭时不叠加，识别成功后可再次提示。子进程继续受现有进程树清理管理，无需新增常驻服务或快捷键。

## 已知维护项

一次现场日志只记录 `Service cleanup failed`，原因未被详细保存，不能直接判断是残留还是退出竞争。随后用户重启、自启动、登录及语音输入正常。后续可增加不含敏感数据的退出码诊断，并扩大生命周期自动测试；本版本没有为排查而改动已验收的运行环境。
