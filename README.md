# dolbytool

杜比全景声视频素材工具，Python + PySide6，五类产物分开输出

- 分轨 WAV: 5.1 母带输出 surround / center / lfe / front / front_no_center / downmix / low / high，杜比全景声（E-AC-3 JOC）先渲染为 5.1.4，再多出 height / height_front / height_rear 三条顶部分轨；全景声还可输出带对象元数据的 ADM BWF，详见下文
- 渐变图 PNG: 封面 k-means 取主色作上色，同色相压暗作下色，ffmpeg maskedmerge + smoothstep 生成
- 波形滚动视频 MP4: 沿用原脚本（compand 压缩放大，左右声道上下两条），深色底 + 冷银色波形，红色播放头居中，播放头左侧已播放部分保持原色，右侧未播放部分向背景色淡出（亮度可调，默认 35%）；波形长图预先补好两侧留白，只解码一次，每帧按帧号裁切，3 分钟的歌约 8 秒
- 单行字幕视频 WebM: TTML 逐字歌词交给 AMLL（applemusic-like-lyrics）渲染，无头 Chromium 逐帧截透明画面，编码为 VP9 + alpha，详见下文
- 声场视频 WebM: 全景声动态对象画成房间里的小球，头模居中，按 OAMD 位置移动、按对象电平显隐，可按音轨分别输出，three.js 逐帧渲染，同样是 VP9 + alpha，详见下文

## 运行

界面使用系统 Qt（原生 Breeze 风格与 KDE 文件对话框），PySide6 来自官方源而不是 pip

```bash
sudo pacman -S --needed pyside6 ffmpeg
```

```bash
uv venv --system-site-packages --python /usr/bin/python3
```

```bash
uv run dolbytool
```

可带参数直接载入: `uv run dolbytool <音频> [封面]`

字幕视频与声场视频另需 Node（或 bun）与 ffmpeg 的 libvpx-vp9，首次使用先构建渲染器

```bash
cd renderer && bun install && bun run build
```

首次渲染会自动下载 chrome-headless-shell 到 `~/.cache/dolbytool/chrome`（约 100 MB）

默认编码器 H.264 NVENC，可切换 HEVC NVENC / libx264，所选硬件编码器不可用时自动改用 libx264

## 用法

1 选择或拖入音频（m4a / eac3 / wav 等）与封面图片，音频在后台读取，内嵌封面与同名 `.ttml` 自动填入
2 输出目录默认为音频旁的 `<音频名>_stems`
3 左侧“输出”清单勾选要生成的内容，点选某一项在右侧编辑它的设置；每项下方显示将生成什么，设置有问题时显示红字原因
4 点“开始”（Ctrl+Enter）运行，Esc 取消；下方“任务”页逐项显示进度、用时与结果，双击已完成的任务打开输出文件
5 某项失败时其余任务照常运行，结束后汇总失败原因；多个波形视频最多 3 个同时编码
6 分轨只渲染勾选的分轨（及与它们共用增益的同组分轨）；波形视频勾选了尚未生成的分轨时会自动补做这几条
7 字幕视频只需 TTML，不勾选其他项时可以不选音频
8 输出先写到同目录的 `.<名>.partial.<扩展名>`，完成后再替换，取消或失败不会损坏已有文件；输出与输入同名或两项输出同名时拒绝开始
9 参数、勾选状态与窗口布局会记住，保存在 `~/.config/dolbytool/dolbytool.conf`

## 输出文件

- `<音频名>_<stem>.wav`
- `<音频名>_adm.wav`
- `<音频名>_gradient.png`
- `<音轨文件名>.mp4`
- `<音频名或歌词名>_lyrics.webm`
- `<音轨文件名>_soundfield.webm`

## 杜比全景声

- 音频流为 E-AC-3 JOC（ffprobe 显示 `Dolby Digital Plus + Dolby Atmos`）时自动识别，按 ETSI TS 103 420 自行解码，不依赖杜比或第三方解码器
- ffmpeg 只负责解出 5.1 核心，之后从每帧的 EMDF 容器读取 OAMD（对象位置、增益）与 JOC 参数，在 64 带 QMF 域由 5 个主声道重建对象，再按房间立方体模型等功率平移到 5.1.4
- 编码端对 Ls / Rs 做过 90 度相移（`joc_dmx_config_idx` 为 3），重建前在 QMF 域补回，前后声道相位与原始混音一致
- LFE 不经过 JOC，直接取核心解码的 LFE，与主声道天然对齐
- 输出按容器时间轴裁齐（编辑列表跳过的预热帧会去掉），与原音频、波形视频和字幕逐样本对齐
- 立体声缩混把顶部声道按 -3 dB 并入同侧，环绕声道分轨只含地面层
- 元数据按帧顺序解析一遍，音频按时间段分给多个进程并行重建（每段从上一帧恢复滤波器组、JOC 与增益状态），结果与单进程逐样本一致；3 分钟的歌解码约 6 秒，全部 11 条分轨加 ADM BWF 约 13 秒（20 线程）
- 5.1.4 渲染直接写进磁盘映射缓冲交给分轨，不再写出再读回中间 WAV
- 对象轨迹随每次解码缓存在 `~/tmp/dolbytool/cache`，同一文件之后的声场视频不再重复解码
- 暂不支持带从属子流的码流（7.1 及以上声床）与 TrueHD 全景声，稀疏模式的 JOC 参数按规范实现但没有样本验证过
- 对象的尺寸、区域限制（除关闭高度外）和屏幕参考暂未参与渲染

## 字幕视频

- 当前行固定在画面中央，换行时旧行淡出、新行淡入，新行在第一个字开唱时刚好完全显示；行唱完后最多停留 1 秒
- 字体可选多个并排序，每个字取列表中第一个含该字形的字体，列表为空时用系统无衬线字体；中文字体一般自带西文字形，要让西文用别的字体就把西文字体排在前面
- 翻译行亮度可调，默认 70%（AMLL 原样式 30%，叠在画面上太淡）
- 只显示主歌词与翻译，音译（整行与逐字）、背景人声丢弃，对唱行也居中
- 时长跟随已载入的音频，便于与波形视频对齐；没有音频时到最后一行结束
- 渲染在确定性模式下进行（BeginFrame 控制时钟），同样参数两次输出逐帧一致
- 时间轴切段后由多个浏览器并行渲染再无损拼接，默认每 2.5 个 CPU 核一个（本机 8 个），每段至少 10 秒；每段先从当时在屏的行开始淡入处预跑，结果与单个浏览器逐帧一致
- 前奏、间奏和结尾没有歌词在屏时直接复用透明帧，不经过浏览器；VP9 用 realtime 预设（cpu-used 6），3 分钟 60fps 的歌约 27 秒渲染完
- 设置环境变量 `DOLBYTOOL_DEBUG=1` 时渲染器在 stderr 打印各浏览器与分段的耗时
- 剪辑软件需识别 VP9 alpha 才能透明叠加，ffmpeg 叠加时解码器要指定 `-c:v libvpx-vp9`

## ADM BWF

- 分轨面板勾选“ADM BWF”后输出 `<音频名>_adm.wav`，只对杜比全景声有效，可以不勾任何分轨单独输出
- 与 5.1.4 渲染同一遍解码完成，几乎不增加耗时
- 声道 k 即 OAMD 对象 k: 床声道写成 DirectSpeakers（LFE 取核心解码的 LFE），ISF 与动态对象写成 Objects；对象自身增益（含渐变）已乘进音频，元数据不再带增益
- `axml` 为 ITU-R BS.2076-2，ADM 笛卡尔坐标（X 左 -1 到右 +1，Y 后 -1 到前 +1，Z 听音平面 0 到顶 +1），每次位置更新一个 block，`jumpPosition` 的 `interpolationLength` 即 OAMD 渐变时长；`chna` 为标准 40 字节条目
- 24 bit PCM（格式标签 1，不用 WAVE_FORMAT_EXTENSIBLE，兼容的读取器更多），超过 4 GiB 时自动转为 BW64（预留的 JUNK 改写为 ds64）
- 用 ADM 块位置重新渲染与本工具的 5.1.4 输出相比，误差比信号低 36 到 50 dB（差异来自忽略渐变的简化渲染），可直接导入 ADM 对象查看器
- 对象的尺寸、区域限制与 snap 暂不写入元数据

## 声场视频

- 画面照搬全景声对象查看器: 半透明房间（5.8 × 3.1 × 5.8）、地面网格、指向正面的箭头、站在地板中央的头模，统一灰蓝色，不画背景色
- 只画动态对象，床声道固定在扬声器上、ISF 没有位置信息，都不画
- 像波形视频一样勾选音轨，每条输出一个视频（默认原音频），不需要先分轨；分轨视频照样画出全部有声对象，轨迹连贯，只是声音没落在该分轨扬声器上的对象变暗: 对象电平只计入渲染器分给这些扬声器的能量，低于静音门限时按淡入淡出时长过渡到“分轨外亮度”（默认 25%，设为 0% 即隐藏）
- 各音轨对应的扬声器: 原音频与 downmix 为全部，surround 为 Ls Rs，center 为 C，front 与 front_no_center 为 L R，height 为四个顶部，height_front / height_rear 为前后两对；lfe、low、high 不在房间里有位置，不列出
- 5.1.4 渲染按位置在前后两排之间等功率平移，只有贴着前墙（y=0）的对象完全不进 Ls / Rs，靠前但不贴墙的（如 y≈0.3）仍有约 -6 dB，在 surround 视频里不会变暗；height 视频里听音平面上的对象都会变暗
- 对象位置取自 OAMD，按 ramp 线性滑动；房间坐标 x 左到右、y 前到后、z 听音平面为 0，与 ADM 惯例一致，听音平面的对象在房间半高处
- 显隐按 JOC 重建后的对象电平（乘上对象增益后的峰值），低于静音门限（默认 -90 dBFS）或对象关闭时在 180 ms 内淡出，与查看器一致
- 视角三选一: 等轴测（默认）、顶部（正面在画面上方）、Angle（对面低角度）
- 头模可选 `.glb`（three.js GLTFLoader，自动转向正面、落地、统一着色），不选则用内置头部剪影
- 先取对象轨迹（与分轨共用全景声解码并缓存，未缓存时约 6 秒，多条音轨只解析一次），再逐帧渲染；每帧是时间的纯函数，分段并行无需预跑，结果与单个浏览器一致
- WebGL 走 ANGLE Vulkan 用独显渲染，同样参数两次输出逐帧一致；没有可用 GPU 时自动退回 SwiftShader，慢 2 到 3 倍（GL-EGL 后端每次结果不同，不要用）
- 1080p 60fps 的 3 分钟歌本机约 100 秒（RTX 3070 Ti Laptop）
- `DOLBYTOOL_DEBUG=1` 时渲染器在 stderr 打印实际使用的 WebGL 后端

## 目录

按功能组织，每个功能一个包，纯逻辑放 `service.py`（不依赖 Qt），界面放 `panel.py`；功能之间不互相导入，只通过 `core.project.PlanContext` 交换信息（如波形视频需要尚未生成的分轨）

- `src/dolbytool/core/` 各功能共用的基础: `ffmpeg.py` ffmpeg / ffprobe 调用，`renderer.py` 运行 `renderer/dist` 下的脚本并解析进度，`process.py` 可取消的子进程，`cancel.py` 取消令牌，`pcm.py` 磁盘映射的音频缓冲与 WAV 读写，`paths.py` 临时目录、缓存与原子写出，`audio.py` 音频信息，`project.py` 当前输入与音轨列表，`task.py` 任务步骤，`stems.py` 分轨种类、各声道布局对应的分轨与中文名
- `src/dolbytool/features/source/` 源文件: `service.py` 音频探测、内嵌封面、同名 TTML，`panel.py` 后台读取与拖放
- `src/dolbytool/features/stems/` 分轨: `pipeline.py` 按声道布局渲染各分轨，`dsp/` 分频、声道矩阵与频谱中置分离，`service.py` 解码与任务封装
- `src/dolbytool/core/atmos/` 全景声解码（分轨与声场视频共用）: `eac3.py` 切帧，`emdf.py` 元数据容器，`oamd.py` 对象元数据，`joc.py` 对象重建，`qmf.py` 滤波器组，`render.py` 5.1.4 渲染，`sinks.py` 每帧渲染结果与 5.1.4 声道，`adm.py` ADM BWF，`trace.py` 对象轨迹、电平与各扬声器能量占比，`chunk.py` 工作进程解码一段时间轴，`decoder.py` 元数据遍历、分段调度、时间轴对齐与轨迹缓存，`bitstream.py` 位读取与查表 Huffman，`tables.py` 规范附带的 Huffman 表与 QMF 系数
- `src/dolbytool/features/gradient/` 渐变图与封面取色（`palette.py`）
- `src/dolbytool/features/waveform/` 波形视频，编码器列表在 `encoders.py`
- `src/dolbytool/features/lyrics/` 字幕视频，调用 `renderer/dist/render.mjs`
- `src/dolbytool/features/soundfield/` 声场视频，先取对象轨迹再调用 `renderer/dist/soundfield.mjs`
- `src/dolbytool/features/registry.py` 输出面板的顺序，新增功能在这里登记一行
- `src/dolbytool/ui/` 界面外壳: `main_window.py` 只负责组装与运行，`feature.py` 输出面板的公共接口，`outputs.py` 输出清单，`tasks.py` 任务列表，`worker.py` 后台任务队列（并行组与失败后继续），`background.py` 后台调用，`widgets.py` 通用控件（含音轨勾选列表）
- `renderer/src/shared/api.ts` 页面与命令行之间的接口定义
- `renderer/src/page/` 字幕页面: `timeline.ts` 每行的显隐时间（纯函数），`stage.ts` 每行一个 AMLL 播放器
- `renderer/src/soundfield/` 声场页面: `timeline.ts` 对象位置与显隐（纯函数），`scene.ts` 房间、头模、相机，`main.ts` 入口
- `renderer/src/cli/` 命令行: `chrome.ts` 下载与启动 chrome-headless-shell，`stage.ts` 逐帧驱动浏览器，`segment.ts` 分段渲染，`parallel.ts` 分段并行与拼接，`ffmpeg.ts` 编码与拼接，`run.ts` 取消与清理，`main.ts` 字幕入口，`soundfield.ts` 声场入口

渲染器类型检查: `cd renderer && bun run check`

## 许可

字幕渲染依赖 AMLL（AGPL-3.0），整体按 AGPL-3.0-or-later 分发
