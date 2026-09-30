# 麦麦假装看视频

麦麦插件。群里有人发视频，它会自己看懂，然后把内容记在心里——
之后你问相关的事，它答得上来。**不会主动对视频发表评论。**

- 抽多少帧由画面内容决定，不是定时抽 —— 重复内容不浪费
- 单条视频约 **1 分钱**，重复视频 **0.4~0.6 秒**复用，跳过模型调用
- 看得懂时间点：「视频 10:03 那里是什么」能回答
- 可选回看某一秒：开启保留原片后，模型能自己回去看指定区间

---

## 视频取回方式

### SnowLuma 适配器

使用 MaiBot SnowLuma Adapter 时，插件会通过 `adapter.snowluma.message.get_msg`
读取原始消息里的视频 URL，再由现有下载流程取回原片。**不需要安装本仓库的 NapCat
辅助插件，也不需要新增视频取回配置。**

### NapCat：配套下载插件

**只有使用 NapCat 适配器时，才需要配套的 NapCat 插件。** QQ 收到视频通常不会把
原片下载到服务器；消息适配器也会把视频段压成文本占位并丢掉下载地址。因此需要本仓库
自带的 NapCat 插件（`napcat-plugin/video-fetch/`）：它监听视频消息，把原片下载到本地，
供本插件取用。

**安装步骤（使用 Napcat 适配器时缺一不可，使用 SnowLuma 时请忽视）**：

1. 把 `napcat-plugin/video-fetch/` 整个目录放进麦麦的 `napcat/plugins/` 下
2. 在 `napcat/config/plugins.json` 写入 `{"video-fetch": true}`
   —— NapCat 除内置插件外**默认禁用**，不写这个文件就不会加载
3. **重启 NapCat**（会短暂影响 QQ 在线，建议挑冷清时段）
4. 本插件配置 `napcat.fetch_dir` 填上那个下载目录的绝对路径
5. 发个视频验证：该目录下的 `latest.json` 应变成 `phase: done`

## 安装

1. 把本仓库整个目录放到麦麦的 `plugins/` 下（目录名建议 `video-understanding`）。
   **仓库根目录就是插件本体**，根下直接有 `_manifest.json`。
2. 使用 NapCat 适配器时，按「NapCat：配套下载插件」一节安装辅助插件；使用 SnowLuma
   Adapter 时跳过此步。
3. 在 WebUI 启用本插件。
4. **`extract.ffmpeg_path` / `extract.ffprobe_path` 填绝对路径。**
   麦麦进程继承的 PATH 可能是很久以前的，`shutil.which("ffmpeg")` 会返回 `None`，
   导致抽帧报 `WinError 2`。这一项别偷懒。

## 怎么用

不用管它。有人发视频，它在后台自己处理，处理完就知道了。
想确认它有没有看懂，像平时聊天一样问就行。

---

## 专为低消耗设计

| 维度 | 做法 | 效果 |
|---|---|---|
| 钱 | 自适应抽帧 + 便宜的视觉模型 | 单条约 **1 分钱** |
| 算力 | 帧数由内容算出来 | 5 分钟循环视频只抽 3 帧 |
| 重复开销 | 签名缓存复用描述 | 第二次 **0.4~0.6 秒**，跳过模型调用 |
| 内存 | 加载本地模型前先查可用内存 | 内存不够自动降级，不会把宿主挤崩 |
| 磁盘 | 临时文件用完即删 | 不堆文件 |
| 延迟 | 后台处理，不阻塞聊天 | 约 6 秒出结果 |

关键在**抽帧**：常见做法是每隔几秒截一张，不管画面里在演什么；
本插件按画面的信息密度分配帧位——变化剧烈处密、几乎不动处疏。
所以重复画面不浪费帧，关键画面也不会漏。

## 时间点问答

理解时会产出**带时间戳**的分段描述，所以「某一时刻是什么」这类问题有据可答。

问得很细时（比如「第 10 秒那人穿的什么」）摘要可能不够，可以开启**保留原片**，
插件会额外给模型一个回看原片的工具：

```toml
[timeline]
keep_video = true          # 理解完把原片备份到 kept_videos/
keep_video_hours = 24      # 保留时长，超时自动删
```

开启后，模型会自己判断要不要回看，以及看哪一段。实测（老服务器）：

> 问「视频第 4 秒是什么」→ 它回看 3~6 秒 → 回答「第四秒还在电脑前，白T恤那哥们坐椅子上操作呢」

**代价**：原片按小时数占磁盘（一条几 MB 到几十 MB），每次回看一次模型调用。
只在模型觉得需要时才触发。

## 配置

| 分组 | 字段 | 默认 | 说明 |
|---|---|---|---|
| 插件 | enabled | true | 总开关 |
| 抽帧 | ffmpeg_path / ffprobe_path | 空 | **建议填绝对路径** |
| 抽帧 | seconds_per_frame | 4.0 | 每几秒一帧 |
| 抽帧 | min_frames / max_frames | 0 / 0 | 0 表示自适应 |
| 音频 | mode | off | off 不读 / local 本地模型 / api 云端接口 |
| 音频 | api_key / api_url / api_model | 空 | api 模式用 |
| 音频 | local_model / local_tokens | 空 | local 模式用（见下） |
| 音频 | min_free_mb | 500 | 内存闸门：可用内存低于此值就不加载本地模型 |
| 音频 | fallback | true | api ↔ local 失败自动回退 |
| 音频 | unload_after | true | 转录完卸载本地模型 |
| 音频 | block_seconds | 120 | 单块音频最长秒数（越大调用越少） |
| 音频 | gap_seconds | 6 | 停顿短于此值就并成一块（越大越省调用） |
| 音频 | max_audio_seconds | 600 | 只转录前 N 秒，0 表示不限 |
| 时间轴 | enabled | true | 产出带时间戳的分段描述 |
| 时间轴 | ttl_days / max_entries | 7 / 200 | 时间轴保留天数与条数上限 |
| 时间轴 | keep_video | false | 保留原片，解锁回看（见「时间点问答」） |
| 时间轴 | keep_video_hours | 24 | 原片保留小时数 |
| 时间轴 | inject_fresh_seconds | 300 | 理解完这段时间内每条请求都带上内容；之后仅在提到视频时带（省 token） |
| 视觉 | mode | host | host 走主程序任务 / direct 直连接口 |
| 视觉 | api_url / api_key / model | 空 | direct 模式用 |
| 缓存 | enabled / match_threshold | true / 0.9 | 相似视频复用描述 |
| NapCat | fetch_dir | 空 | NapCat 专用：取回插件的下载目录，建议填；SnowLuma 不使用 |
| NapCat | fetch_wait_s | 60 | NapCat 专用：等下载完成的最长秒数 |
| NapCat | fetch_keep_hours | 24 | NapCat 专用：取回目录里文件的最长保留小时数 |
| 视频源 | cleanup_after | true | 理解完成后删除视频与中间文件 |
| 视频源 | max_video_mb | 80 | 超过该体积跳过 |
| 视频源 | max_videos_per_message / concurrency | 3 / 1 | 单条消息上限 / 同时处理数 |

## 音频（可选）

默认 `audio.mode = off`，**不读音频**。想读有两种：

**云端**：把 `mode` 设为 `api`，填 `api_key`（音频会上传到服务商）。

**本地**：`pip install sherpa-onnx`，再准备 SenseVoice 模型（`model.int8.onnx` 约 228MB
和 `tokens.txt`），路径填进 `local_model` / `local_tokens`。不外传、无调用费。

⚠️ **本地模式吃的是 CPU，不是内存。** 桌面级 CPU 上能跑到 25~40 倍实时，
但弱 CPU（比如赛扬双核）只有 3~8 倍，还要和抽帧抢核。
**内存小可以开本地，CPU 弱反而该用云端。**

## 已知限制

先说清楚做不到什么，免得白等：

- **合并转发暂不支持**。群里那种「合并转发」的消息，插件拿不到里面的视频
- 转发消息里若只带发送方的手机路径，服务器上不存在该文件，会直接跳过
- 时间轴粒度跟着画面复杂度走，**静止片段的描述会比较粗**。
  要问得很细，请开 `keep_video` 走回看
- **隔很久再问需要引用原消息**。只口头说「刚才那个视频」、而且上下文已经过期时，
  它定位不到是哪条
- 同一时间有多个视频、你只说「那个视频」时，靠模型自己判断，拿不准它会先问你
- SnowLuma 需要适配器的 `get_msg` 返回原始视频段和可用 URL；NapCat 需要配套下载插件
- 需要自己准备：ffmpeg；使用 NapCat 时还需配套插件；（可选）sherpa-onnx

## 依赖

- ffmpeg / ffprobe（必须，建议配绝对路径）
- 主程序 1.0.0+，SDK 2.0.0+
- NapCat 适配器：配套 NapCat 插件（本仓库 `napcat-plugin/video-fetch/`）
- SnowLuma 适配器：无需额外取回插件或新增取回配置
- 可选：sherpa-onnx（本地音频模式）

## 许可

[PolyForm Noncommercial 1.0.0](LICENSE) —— **非商用许可，不是开源协议**。

- **可以**：使用、修改、分发（包括闭源分发）
- **可以**：个人使用、研究学习、公益 / 教育 / 政府等非商业用途
- **不可以**：任何商业用途——无论是原样还是改过的，开源还是闭源
- 分发时必须一并附上 `LICENSE`
