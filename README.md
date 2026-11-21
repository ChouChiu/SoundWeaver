# SoundWeaver

SoundWeaver 是一个多声道与杜比全景声（Dolby Atmos）制作工具：从 E-AC-3 JOC、AC-4、360RA
和 Audio Vivid 码流提取分轨与 ADM BWF 母带，并生成 Apple Music 风格动态歌词、完整播放器、
滚动波形与 3D 空间声场视频。

## 快速上手

### 系统依赖
- **Python** 3.11+
- **FFmpeg**（需启用 `libvpx-vp9`；推荐支持 `h264_nvenc` / `hevc_nvenc` 硬件加速）
- **Bun** 1.1+（构建渲染器）
- **CMake + Ninja**（构建原生音频组件时需要）

### 安装与运行
```bash
uv sync                                  # 1. 安装 Python 依赖
cd renderer && bun install && bun run build && cd ..   # 2. 编译渲染器（首次运行前）
uv run python scripts/build_native.py    # 3. 构建 360RA / Audio Vivid 原生组件
uv run soundweaver                       # 4. 启动
```

> 首次生成视频时会自动下载配套的 `chrome-headless-shell`（约 100 MB，存放在 `~/.cache/soundweaver/chrome`）。

### 环境变量

| 变量 | 作用 |
| :--- | :--- |
| `SOUNDWEAVER_DEBUG=1` | 在终端输出浏览器切片耗时与 WebGL 诊断日志 |
| `AV3A_DECODER_BIN` / `AV3A_ENCODER_BIN` | 指定 Audio Vivid 解码 / 编码可执行文件路径 |
| `MPEGH_DECODER_BIN` | 指定 MPEG-H 解码可执行文件路径 |
| `SOUNDWEAVER_BIN_DIR` | 指定原生组件所在目录，替代内置 `bin/` |

### 运行测试
```bash
uv run pytest            # 全部测试；原生组件缺失时相关用例自动跳过
uv run ruff check .      # 静态检查
cd renderer && bun run check   # 渲染器类型检查
uv run python scripts/ui_snapshots.py /tmp/ui   # 离屏截取每个页面，用于布局走查
uv run python scripts/measure_atmos_latency.py <file.m4a>      # Atmos / AC-4 A-JOC 首声延迟与分段发布
uv run python scripts/check_grow_while_playing.py <file.m4a>   # ADM 边播边长的读取验证（同样适用 A-JOC）
```

## 主要功能

### 杜比全景声与音频分轨

- **内置 E-AC-3 JOC 软解**：基于 ETSI TS 103 420 直接从码流重构空间音频对象，支持流媒体常见的 5.1 核心下混 JOC 码流
- **内置 AC-4 软解**：按 ETSI TS 103 190 实现（频谱解码、立体声与多声道矩阵、IMDCT 块切换、A-SPX、A-CPL），解码结果缓存为 FLAC 供分轨、波形与播放器视频复用；支持裸 `.ac4` 码流。解码结果已与同曲 E-AC-3 版本及 Dolby 测试码流的 AAC / DD+ 参考对照验证
- **AC-4 杜比全景声（A-JOC）**：对象由下混信号经 A-JOC 重建，复用 E-AC-3 全景声的同一套渲染器，扬声器渲染、声场视频与 ADM BWF 导出均可用；已用真实母带验证 7.1.4 渲染逐声道相关度与电平一致性。沉浸式声道元素（声道模式 11 以上）暂不支持
- **12 种渲染布局**：2.0、5.1、7.1、5.1.4、7.1.4 到 9.1.6，按对象位置做恒功率房间声像；渲染到 2.0 / 5.1 / 7.1 时套用码流自带的 trim 修剪并遵守对象的区域约束；ISF 对象按环形布局定位
- **常用分轨提取**：人声（中置）、伴奏（前置去人声）、环绕、顶置、低频（LFE）及高低频分音；立体声音源支持 Mid/Side 与人声伴奏分离
- **零相位分频**：高低频分轨各为 24 dB/oct、交越点 -6 dB，二者相加逐样本还原且互无延迟
- **ADM BWF 母带导出**：默认 Dolby Atmos Master ADM Profile v1.1，含空间对象轨迹；勾选「移除静音声床占位」可将通用 ITU ADM（保留独立 LFE）另存为 `*_adm_compact.wav`
- **360RA ADM 还原**：从 MPEG-H 码流取出对象信号、位置、增益与扩散，按 ITU-R BS.2076 写成 Objects 轨道（每帧一块、带 spread 宽度/高度/深度），与容器时间轴逐样本对齐；码流只有声道床时改写为对应布局的 DirectSpeakers 床。已用 Apple Music 360RA 曲目验证与解码渲染逐点相关 1.00
- **相位校正与容错**：自动补偿 Ls/Rs 的 90° 编码相移；按 Edit List 裁齐时间轴消除预热延迟；码流损坏时报出所在帧号

### 视频可视化生成

- **Apple Music 风格播放器视频**（`.mkv` / `.webm`）：横屏左封面右歌词、竖屏自适应手机布局；Mesh Gradient 流体渐变（可随 50–120 Hz 低频律动）、动态色块与透明背景；逐帧 FFT 频谱、曲目信息、进度条与 Dolby Atmos 徽标；自动过滤歌词首尾的制作人员与版权声明
- **逐字动态歌词视频**（`.webm`）：单行居中动态字幕，逐字平滑高亮与淡入淡出，VP9 透明通道可直接叠加进剪辑轨道
- **3D 空间声场可视化**（`.webm`）：实时展示全景声与 360RA 对象的三维位置、电平光晕与运动轨迹，支持按分轨联动暗化
- **水平滚动波形**（`.mp4`）：双声道上下分轨滚动，中央固定播放头，左侧高亮、右侧暗化，经动态范围压缩强化细节
- **封面渐变背景**（`.png`）：k-means 提取封面主色调并向下平滑压暗，用作视频底图

### 批量与专辑模式

- **歌词自动匹配**：批量导入时扫描同级及子目录的 `.ttml`，清理音轨号与括号后缀后与歌曲元数据交叉比对
- **整张专辑无缝连播**：整张专辑渲染为一个连续长视频，音频无损拼接，切歌时封面、歌词与背景平滑过渡
- **动态封面**：无声循环视频（`.mp4` / `.mov` 等）可作动态封面，自动抽帧并在各并行分段中精确循环

## 常用操作与快捷键

| 操作 | 方式 / 快捷键 | 说明 |
| :--- | :--- | :--- |
| **导入素材** | 直接拖拽 | 音频（`.m4a`（含 AC-4）/ `.ac4` / `.eac3` / `.wav` 等）、封面图片、动态视频封面或 `.ttml` 歌词 |
| **开始任务** | `Ctrl + Enter` | 将勾选的产物加入队列并开始处理 |
| **中止任务** | `Esc` | 取消当前正在执行的任务 |
| **添加单曲** | `Ctrl + Shift + B` | 在批量页面添加单曲音频 |
| **导入专辑** | `Ctrl + Shift + M` | 导入整个专辑目录并开启专辑模式 |
| **打开产物** | 双击已完成任务 | 在任务与日志面板用系统默认程序打开输出文件 |

## 性能与渲染机制

- **播放器分段预读**：普通音频、360RA、Audio Vivid 和 AC-4（含 A-JOC 对象流）先缓冲约半秒即可出声，后台预读约 6 秒；已解码片段保留在临时文件供跳转，切歌与解码关闭不阻塞界面。Atmos 与 AC-4 A-JOC 对象播放边解码边发布：元数据先以首个分段的部分时间线发布，对象轨迹随解码推进原位补全
- **GPU Vulkan 硬件合成**：页面与 Three.js 场景通过 ANGLE Vulkan 启用显卡渲染，分段浏览器分时调度 GPU
- **确定性分段并行**：无头 Chromium 基于 CDP `BeginFrame` 确定性时钟切片并行渲染，渲染完毕后无损拼接，段落间无接缝跳变；无歌词区间复用透明关键帧跳过空跑
- **快速截图**：不透明视频默认 JPEG（质量 90）帧流，截图耗时约为 PNG 的一半（720p 播放器片段 CRF 18 编码后 SSIM 0.997）
- **硬件编码自适应**：H.264 NVENC / HEVC NVENC / libx264，超出硬件尺寸限制（H.264 最大 4K、HEVC 最大 8K）或无显卡时回退软件编码
- **分块渲染防溢出**：视频单边最高 16384 px；3D 声场超过 8K 自动分块渲染再拼接
- **写入保护**：产物先写入目标目录内的独立临时目录，成功后原子替换，中断时保留已有文件

## 项目结构

功能驱动组织，每个功能自成一片：

- `features/`：`source` 与 `batch` 将素材整理为 `Project`；`monitor` 管理播放列表、播放条与空间监视；`tasks` 管理任务视图、日志与 Qt 工作线程。分轨、渐变、波形、歌词、播放器视频与声场各有 `panel.py`（Qt 面板）和独立于 Qt 的 `service.py`（生成并执行输出步骤），功能之间不互相引用，只通过 `core.project.PlanContext` 通信
- `ui/`：共用 Fluent 控件、主题色、字体、间距与键盘焦点样式；`feature_registry.py` 注册输出面板并规定顺序，`export_workflow.py` 组装单曲与批量导出预检，主窗口负责导航、确认对话框与执行状态｜[Fluent 2 布局](https://fluent2.microsoft.design/layout) · [可访问性](https://fluent2.microsoft.design/accessibility)
- `core/`：`planning.py` 定义规划合约与路径检查，`runner.py` 执行任务并控制并行与取消；`playback_source.py` 按格式准备 PCM 与对象元数据，`playback_mix.py` 负责混音与电平，`playback.py` 管理 Qt 音频设备；`atmos/` 与 `ac4/` 是两套规范实现的位流解码器
- `core/codecs/` 与 `native/`：编解码原生组件（交接与迁移结论见 [docs/HANDOFF.md](docs/HANDOFF.md)）；`adm_bwf.py` 是 ADM 写入器共用的容器层，`mpegh_adm.py` 负责 360RA 对象轨道与声床
- `renderer/src/`：按歌词（`page/`）、播放器（`player/`）、声场（`soundfield/`）与实时监视（`monitor/`）组织，共用场景工具与浏览器导出流程在 `shared/` 与 `cli/`

## 开源许可

本项目采用 [AGPL-3.0-or-later](LICENSE) 开源许可证。
歌词与播放器渲染基于 [applemusic-like-lyrics](https://github.com/Steve-xmh/applemusic-like-lyrics)（AGPL-3.0）；
`native/` 内的 MPEG-H 解码器为上游 fork，许可证见 `native/mpegh/LICENSE` 与 `native/mpegh/LICENSE2`，
Audio Vivid 组件的上游来源见 `native/av3a/UPSTREAM.md`。
