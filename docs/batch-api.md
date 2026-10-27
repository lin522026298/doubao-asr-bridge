# 批量转写与缺失区间

`POST http://127.0.0.1:18765/v1/audio/transcriptions` 使用 multipart，必填 `file`：16 kHz、单声道、PCM16 WAV，单次最多五分钟。客户端同时只发一个请求；429 或连接故障时不要并发重试。

## 请求字段

| 字段 | 默认值 | 含义 |
|---|---|---|
| `file` | 必填 | WAV 音频；无转写正文或文件名写入诊断日志 |
| `audio_offset_seconds` | `0` | 该文件在原录音中的起点，有限非负秒数，上限 1,000,000,000 |
| `allow_partial` | `false` | 接受 `true/false/1/0`；只有明确开启时，不完整但有可用子段的结果才可返回 HTTP 200 |

例如从原录音第 58 秒截取的音频，应发送 `audio_offset_seconds=58`。所有 `segment_results` 和 `missing_ranges` 中的起止秒数均已加上此偏移，客户端不要再重复相加。内部子段以 PCM 样本边界覆盖整个文件，无额外重叠或摘要去重；输入文件间已有重叠仍由调用者处理。

## 响应与 HTTP 状态

| 情况 | HTTP | 关键字段 |
|---|---|---|
| 所有子段有结果，或可证实为全零数字静音 | 200 | `complete=true`、`partial=false`、`missing_ranges=[]` |
| 部分完成，默认严格模式 | 502 | `status=partial`、`complete=false`、保留成功文本及缺口、`error.code=partial_transcription` |
| 部分完成，显式 `allow_partial=true` | 200 | 仍为 `complete=false`、`partial=true`，必须处理缺口 |
| 全部无可用转写 | 422 | `status=failed`、`complete=false`、`error.code=no_transcript` |
| 全部失败且存在其他上游/网络/登录原因 | 502 | 逐段错误、缺口和恢复方式 |
| WAV、偏移或控制字段不合法 | 400 | `error.code=bad_request` |
| 其他请求正在识别 | 429 | 停止并发调用，稍后串行尝试 |

**必须同时检查 HTTP 与 `complete`。** 200 不一定完整，因为知情客户端可以显式接受部分结果。默认严格模式保留 502，避免只读取 `text` 的旧客户端把缺失内容静默当成完整转写。即使收到 502，也先解析 JSON，保留已成功的子段及 `missing_ranges`。

完整响应若 `text` 为空且全部为 `silence`，客户端应记为无输入并停止后续润色或发布，不应让大语言模型凭空补充内容。

`complete` 只表示每个内部子段均被记为已识别或可证明的全零静音，不是逐字正确率或无漏字保证。上游仍使用 finish 事件或结果静默启发式，噪声中的词语可能被模型漏掉；本适配器不能证明这些词已经完整转写。

响应示意（实际切点由音频决定）：

```json
{
  "status": "partial",
  "complete": false,
  "partial": true,
  "allow_partial": false,
  "text": "first segment third segment",
  "audio_offset_seconds": 120,
  "audio_seconds": 60,
  "segments": 3,
  "recognized_segments": 2,
  "silence_segments": 0,
  "failed_segments": 1,
  "missing_ranges": [
    {
      "segment_index": 1,
      "start_seconds": 139.55,
      "end_seconds": 157.3,
      "code": "no_transcript",
      "retryable": false,
      "recovery": "review_audio_or_use_alternative_asr"
    }
  ],
  "error": {
    "code": "partial_transcription",
    "message": "仅部分子段完成识别；请检查 missing_ranges，不能将本结果当作完整转写。",
    "retryable": false,
    "recovery": "recover_missing_ranges"
  }
}
```

此外，每次返回独立 `request_id`、总耗时，以及 `segment_results`。每段包含 `index`、全局起止时间、音频秒数、状态和耗时；`ok` 段包含正文，`failed` 段包含经过限制的错误及协议统计。`text` 仅拼接 `ok` 段，**其中没有自动插入缺口占位符**；制作最终稿时，应按逐段顺序保留缺口标记，不要直接把这个拼接字符串发布为全文。

## 静音、噪声与恢复

- 只有 PCM 每个样本严格等于零的子段可在本地记为 `silence`，不会上传上游；全零输入可得到完整的空文本响应。
- 非零的低音量、背景噪声及上游空文本都不能证明没有语音。未获得非空文本时记为 `no_transcript`，保留缺失区间；不会触发登录过期弹窗。
- `no_transcript` 的 `retryable=false` 表示不建议无条件重复相同请求；可核听、重新分段或用其他 ASR 恢复缺口，并不表示音频内容永远无法恢复。
- 连接提前关闭、网络错误、子段超时和总截止超时可标为 `retryable=true`，表示允许有限重试，不保证重试成功。登录拒绝需先处理登录或权限。
- 一个子段失败不会取消其他子段。最长三个上游子请求并行；子段超时预算为音频秒数加 25 秒，总预算为请求音频秒数加 40 秒。总截止后取消未完成任务，保留已完成结果并标出 `batch_timeout` 缺口；调用者取消则清理所有子任务。

## 日志和能力发现

`GET /health` 增加 `batch_result_contract: segments-v1` 与 `recognition_busy`。服务正常或此前会话已验证，不意味着任意一段音频必然能识别。

不完整批次写入安装目录的 `data/batch-diagnostics.jsonl`，按约 1 MB 轮转，最多保留当前文件与一个轮转文件。内容白名单仅包含请求 ID、时间范围、状态、字符数、耗时、错误码及数值协议统计；不保存音频、正文、文件名、Cookies 或带凭据的 URL。对应客户端可凭 `request_id` 定位该次诊断。
