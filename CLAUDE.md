# OpenZilo-HMM

uv 工作区：`hmm-gesture`（`src/`，只做识别推理）与 `hmm-gesture-studio`（`studio/`，桌面平台）。

```bash
uv sync --locked --all-packages
uv run --locked --all-packages hmm-gesture-studio               # 新界面（PySide6）
uv run --locked --all-packages hmm-gesture-studio --ui legacy   # 旧界面（Tkinter）
uv run --locked --all-packages python -m unittest discover -s tests -v
```

无显示器时，Qt 测试自动使用 `QT_QPA_PLATFORM=offscreen`；Linux 需要 `libegl1 libgl1`。旧界面测试还需要 Python 带 Tkinter。

## 两套界面

- **新界面** `studio/src/hmm_gesture_studio/qt/`：所有新功能都做在这里。
- **旧界面** `gui.py`、`sdk_controls.py` 的控件部分、`plotting.py`、`acceleration_view.py`：只保留，不加功能，也不为新界面改动。
- `launcher.py` 选择界面，且在选定前不导入任何一方。
- 依赖方向：`qt/` 可以导入 `device.py`、`training.py`、`datasets.py` 等无界面模块，以及旧模块里的纯函数；旧界面不导入 `qt/`。`hmm-gesture` 不得依赖 Qt。

### 新界面结构

- `controller.py`、`tools_controller.py`：全部状态和规则，不持有控件，仅在主线程运行。worker 和后台线程只向队列放事件。
- `models.py`：图表数据与录制分析，不依赖 Qt 控件。
- `pages/*`：每个页面在 `render_state()` 里从控制器读状态。启用/禁用规则写在控制器的属性上，不写在页面里。
- `theme.py`：所有颜色和 QSS，其他文件不写颜色值。`widgets.py`、`charts.py`：可复用控件和图表。
- 对话框经 `dialogs` 注入。模态框会运行嵌套事件循环，**确认之后必须重新检查状态**，再操作文件或硬件。
- `PySide6-Essentials` 约束为 `<6.12`：6.12 在 Python 3.10 上退出时崩溃（`free(): invalid size`）。放宽前要在 3.10 上验证退出码。

### 联网

应用只在打开“关于”页时访问 GitHub 公开接口（`qt/contributors.py`），不在启动时联网，也不发送设备或手势数据。响应视为不可信，只接受预期域名的 https 链接。测试不得访问真实 GitHub。

### 不能破坏的约束

契约见 `studio/SPEC.md`。其中最容易被 UI 改动破坏的：

- OTA：提交前先加锁；期间不能断开或退出；发送 100% 不算成功；只有 worker 的 `tool_state=idle` 能解锁。
- 手势数据只用原始 int16；校准和诊断值不进入录制与训练流水线。
- 不虚构数值：电量、量程、置信度未知就显示未知，不猜；单类模型不显示置信度。
- 手动裁剪只存标注，不改原始录制；数据或参数变化后，训练出的模型失效。
- 未保存录制、永久删除、中断音频等操作必须确认；未完成的音频不能标记为完整文件。

## 设计规范

视觉基准是根目录的 [DESIGN.md](DESIGN.md)（Apple 产品页风格），令牌定义在 `qt/theme.py`。DESIGN.md 描述的是营销页，Studio 是信息密集的工具，所以按下面的规则落地；两者冲突时以本节为准。

**照搬 DESIGN.md：**

- 颜色只用规范色：`#ffffff` 主画布、`#f5f5f7` 侧边栏和只读文本底、`#1d1d1f` 正文、`#707070` 次要文字。
- **不用阴影，不用渐变。**
- 卡片圆角 28px。
- 按钮和输入框都是胶囊形。字重最高 600，不用 700。
- `#0071e3` 只用于小型主要操作，每组最多一个；`#0066cc` 只用于文字链接。次要操作用透明底加 `#86868b` 描边。

**针对工具的调整：**

| 项目 | DESIGN.md | Studio |
|---|---|---|
| 导航 | 顶部商店导航 | 左侧边栏：采集、训练、实时、诊断、录音、设备工具；底部是“关于”和设备状态卡，点击状态卡进入设备页 |
| 页面标题 | 80px | 40px/600 |
| 区块标题 / 卡片标题 | 40px | 24px/600，17px/600 |
| 正文 / 说明 | 17px | 14px，12px |
| 区块间距 | 90px | 40px |
| 分区背景 | 白色与 `#f5f5f7` 通栏交替 | 全部白色，不用灰色通栏（实际观感突兀）；卡片为白底加 1px `#e6e6e8` 细线 |
| 主角 | 产品图 | 实时图表和识别结果；关键数值用 `StatTile` |
| 控件高度 | 紧凑 | 30px（QSS 圆角不能超过高度一半，否则失效；QSS 的 `min/max-height` 会覆盖 `setFixedSize`，按钮尺寸要写在样式表里） |
| 标题栏 | — | macOS 上融合进窗口（`ExpandedClientAreaHint` + `NoTitleBarBackgroundHint`，要求 Qt ≥ 6.9）：侧边栏背景通到顶，内容避开系统上报的安全区；其他系统保持原生标题栏 |
| 主题 | 浅色 | 只有浅色，不跟随系统深色模式 |
| 字体 | SF Pro | 系统字体回退链（`theme.FONT_FAMILIES`）。SF Pro 不能随包分发，不打包字体 |

**规范之外新增的颜色**（DESIGN.md 没有，工具又必须有）：

- 三轴 X/Y/Z 固定为 `#0066cc`、`#c2410c`、`#0f8a5f`，顺序不变。这组颜色已通过色盲校验，改动时要重新校验。
- 状态色：成功 `#248a3d`，警告 `#b64400`，危险和录制中 `#d70015`，必须配文字，不能只靠颜色表达。
- 识别框和训练框：`#8944ab` 虚线；过短的框用危险色。

**图表：**

- 每个面板只有一条 y 轴。加速度和陀螺仪分成两个面板，不用双 y 轴。
- 网格只画主刻度，并保持弱化；曲线线宽 2px，原始数据用浅色虚线。
- 多条曲线必须有图例；悬停时显示十字线和数值。
- 关闭图表的鼠标平移和缩放，水平拖动只用于框选训练窗口。
- 坐标轴和标题要说明单位与含义（例如"非设备时间戳"）。

**动效：** 克制且有意义，时长取 `theme.MOTION`（220ms）和 `theme.MOTION_FAST`（150ms），工具在 `qt/motion.py`。

- 现有动效：切页渐变、侧边栏高亮滑动、开关滑动、日志展开、进度条平滑、识别结果淡入、录制脉冲点。
- 耗时操作（扫描、连接、训练、验签、音频传输）必须显示 `Busy` 加载标识和说明文字，不能让用户以为点了没反应。结束状态要来自 worker 的明确事件，不能靠猜。
- 循环动画只在控件可见时运行。不要对含图表的页面用 `QGraphicsOpacityEffect`，切页渐变用快照叠加实现。
- 动效不能改变状态生效的时机：页面和控制器状态立即切换，动画只是表现。

**页面分工：** 采集页只显示录制后的波形，不显示实时曲线，实时曲线在“实时”页。日志是列表，每行一条消息，按级别着色并带文字前缀。

**文案：** 界面只有中文。保留测量含义的说明（如"不是去重力加速度""非概率"），不要为了简洁删掉。

**改完界面要看截图：** 用 `QT_QPA_PLATFORM=offscreen` 配合 `window.grab().save(...)` 逐页查看。最小窗口 1100×720 下不能横向溢出（有测试覆盖）。
