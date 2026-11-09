# Python 插件开发与示例指南

本文是 `examples/plugins/` 的 README。每个示例目录都是独立安装单元，包含 `plugin.json` 和 `main.py`，无需复制整个仓库，也不依赖其他示例。接口版本为 `api_version: 1`。

## 信任边界与安装

插件是**任意 Python 程序**，每个插件运行在独立进程及独立 venv 中。进程隔离用于隔离崩溃，venv 用于隔离依赖；两者都**不是安全沙箱**。插件以当前账户权限运行，可以访问文件、网络或启动进程。权限声明是用途告知和确认记录，不是操作系统访问控制；不能声称插件只能访问声明的资源。

建议首次使用步骤：

1. 阅读目标目录的两个文件，核实依赖和权限用途。在新 Qt 界面的插件页选择本地目录安装；选择的是单个示例目录，例如 `examples/plugins/webhook/`。
2. 检查安装来源、固定版本与依赖列表。GitHub 来源应固定到 commit；安装、检查更新、安装依赖由用户手动发起并确认，宿主不在启动时检查更新或安装依赖，也不自动更新。已获准运行的任意 Python 插件仍可能自行联网。
3. 安装声明依赖，再确认信任并启用插件。首次启用和代码更新后重新运行需要确认；新增权限需额外确认。插件启动开关和手势控制开关默认关闭；安装成功不代表已经运行。不要为了测试而自动打开它们。
4. 填写插件设置，先手动测试动作及参数，再创建手势绑定。键盘示例先将焦点放在无敏感数据的空白文档。设置是插件级配置，动作参数属于单次手动操作或具体绑定，不要混用。
5. 确认手势模型和动作。绑定按模型内容指纹隔离，不仅按模型名称匹配。首次成功绑定后，Studio 自动保存完整模型，下次启动自动恢复，无需再次手动导入；仍需手动开启实时识别及手势控制。调整冷却时间，防止重复手势反复触发不可撤销操作。
6. 录音示例还需单独启用完成事件订阅。默认只接收本插件拥有的录音；不回放订阅之前已发生的事件。若扩展为接收其他来源或上传音频，必须另行说明用途并经用户确认，不能沿用普通联网或自身录音授权。

示例已提供可安装结构和离线单元测试；**没有实测 macOS/Windows 媒体键、辅助功能权限、真实接收站点或录音硬件**。Linux 仅适用于不依赖键盘后端的示例；能安装不等于所需录音设备可用。

### 依赖与 wheel

- 键盘、媒体示例固定 `pynput==1.8.1`；Webhook、录音示例只用标准库，无第三方依赖。
- 依赖属于各插件 venv，不应安装到宿主全局环境。不要在 `main.py` 导入时或动作处理期间运行 pip、自动升级或下载依赖。
- manifest 固定直接依赖版本；首次经用户确认安装时，宿主只解析和安装 wheel（含平台相关传递依赖），随后保存 `requirements.lock.json`，重建环境时复用锁定结果。无需在示例 manifest 中手工列出全部平台传递依赖；锁定结果也不是跨平台兼容承诺。第三方依赖仍需纳入信任审查，不代表运行安全。
- 应使用与目标 Python 版本、ABI、操作系统、CPU 架构匹配的 wheel。若某平台没有可用 wheel，不要悄悄回退到本地源码构建；应报告不支持或让用户另外审查构建方案。
- 开发者可在**目标平台的临时 venv** 验证 wheel 可用性，例如执行 `python -m pip download --only-binary=:all: --dest wheelhouse 'pynput==1.8.1'`。该命令会联网，应由用户明确执行；下载结果不是本仓库已验证的跨平台承诺。
- 为避免发现插件时触发平台后端，示例只在实际执行动作时导入 `pynput`。

## 四个示例

| 目录 / 插件 ID | 动作与参数 | 插件设置 | 用途与注意事项 |
| --- | --- | --- | --- |
| `keyboard` / `example.keyboard` | `hotkey(keys)`、`type_text(text)` | `allow_text=false` | `primary+c` 在 macOS 为 Command+C，在 Windows 为 Ctrl+C。最多 5 个组合键；文本最多 1000 字符。发送到当前焦点，不保证目标应用执行成功。 |
| `media` / `example.media` | `send(key, repeat)` | `repeat_interval=0.15` 秒 | 播放/暂停、上下首、音量、静音；次数为 1–5 的整数。切换类按键重复可能抵消效果。 |
| `webhook` / `example.webhook` | `post(payload)` | `url`、`confirmed_target=false`、`token`、`timeout_seconds=5` | payload 是 JSON 对象文本，编码后最多 64 KiB。不会自动附加事件、文件路径或音频。 |
| `recorder` / `example.recorder` | `start(max_seconds=60, live=false)`、`stop()`、`status()` | `analyze_completed=true` | 录音由宿主管理；完整保存后读取 WAV、计算 SHA-256，在数据目录生成 JSON。不上传、不调用收费转写服务。 |

键盘/媒体仅声明 `darwin`、`win32`，执行时也明确拒绝 Linux。macOS 需要在系统隐私与安全性设置中为实际运行的宿主/Python 进程授予辅助功能权限，必要时重启进程；不要关闭系统安全保护。Windows 高权限窗口、系统锁屏等场景可能拒绝模拟输入。媒体键由系统和播放器解释，并不绑定特定播放器。

### Webhook 配置与安全

1. 填写 HTTPS 地址；确认域名、站点运营方、接收用途和隐私政策后，才打开 `confirmed_target`。修改地址后应先关闭此项并重新确认；示例中的复选框不是域名白名单，也不是自动检测地址变化的权限机制。
2. 可选 token 只用于 `Authorization: Bearer …`，不要放进 URL 或 payload。密钥交给宿主的系统凭据库，凭据库不可用时仅保留会话，不能回退为明文配置文件。插件在运行时仍可读取该密钥，这也是必须信任代码的原因。
3. 每次动作发送一次 POST，Content-Type 为 `application/json; charset=utf-8`。动作参数 `payload` 决定实际请求体；不做字符串模板或命令执行。
4. 只接受 HTTPS，不接受 URL 用户信息或 fragment；TLS 使用标准库默认证书验证。不跟随任何重定向，不使用环境代理，不读取或记录响应体，不在错误中包含底层异常、URL 或 token。
5. HTTP 网络操作超时范围为 1–15 秒，默认 5 秒；宿主动作另有 30 秒上限。成功只表示收到 2xx，不证明接收方后续业务成功。超时、连接失败、非 2xx 均不重试；超时时接收方可能已执行。需要去重时在 payload 中自行提供接收端支持的幂等标识，不要盲目重发。

示例不订阅录音事件，也不会读取或上传录音。其 HTTPS 限制是示例代码自律，不是宿主对任意插件的网络限制。

### 录音生命周期与本地结果

- 录音 RPC 必须声明 `recording.control` 权限，包括 start、stop 和 status；旧的 `recording` key 不可替代。`start` 返回 `state='starting'` 和宿主生成的 UUID `session_id`，示例将其记录到 `context.data_dir/session.json`。它不建立设备连接，不绕过 OTA/诊断互斥，也不另外消费 IMU 数据。
- 硬件选项支持 0.02–60 秒且必须为 0.02 秒的倍数；本示例进一步限制 `max_seconds` 为 **1–60 的整数秒**，默认 60 秒，不接受小数秒或超过 60 秒的输入。
- `stop` 只对记录的 session 请求停止，返回 `state='stopping'` 和会话标识。**录音结束不等于保存完成**，starting/stopping 均不是文件完整的证明；不能据此发出 `recording.completed` 或处理半成品。
- 宿主在明确完整保存后才发出 `recording.completed`。默认订阅范围为自身，订阅默认关闭且不回放；用户也可授权“所有新录音”或手动选择已有 WAV。示例信任宿主的订阅 scope 过滤，处理收到的任何完成事件，不再按 owner 排除其他来源。只有 owner 为本插件且 session 匹配时才清理自身 `session.json`。
- `handle_event` 读取 `path` 指向的完整 WAV，不将 `raw_path` 当成 WAV 回退。`raw_only=true` 表示完整 raw 已保存但尚未解码，示例返回跳过结果并记录说明，不假报 WAV 成功，也不报处理失败。WAV 分析会验证音频帧可读性，获取声道数、采样位宽、采样率、帧数和时长，再流式计算整个文件的 SHA-256。原始文件不会被修改或删除；不做语音识别、不联网、不收费。
- 结果保存在 `context.data_dir/recording-<标识的 SHA-256>.json`；标识优先取 session_id，没有 session 时取 event.id，再缺省则生成 UUID，绝不直接用输入拼接路径。结果包含事件原始 owner（可为 null）、session、WAV 元数据、文件哈希、源文件路径和事件提供的时长。WAV 实际时长与事件时长分开记录，不把未知值猜成数字。结果文件包含本地路径，应按敏感数据管理。
- `session.json` 用于跨进程重启恢复停止目标，不是宿主状态的替代品。自动完成、禁用订阅或崩溃可能留下旧 ID；先用 `status` 查询宿主状态，宿主空闲时下次成功 start 会覆盖旧 ID。stop 被宿主拒绝时不会伪造成功。
- 完成事件处理器不能再启动录音，避免事件循环；SDK 与宿主应共同检查这一点。完整事件也不保证文件永远存在，文件被移动、删除或格式不受标准库 `wave` 支持时处理会失败且不会自动重试。

## Manifest：声明设置、参数、权限与订阅

`plugin.json` 是 UTF-8 JSON 对象。通用字段：

| 字段 | 含义 |
| --- | --- |
| `id`、`name`、`version`、`author`、`description` | 稳定插件标识、显示名称、版本、作者、用途说明。发布后不要随意更换 ID。 |
| `api_version`、`entry` | 本例为 `1` 与 `main.py`；入口位于插件安装目录。 |
| `platforms` | Python `sys.platform` 值，如 `darwin`、`win32`、`linux`。不要声明未实现的后端支持。 |
| `requirements` | 明确声明的 Python 依赖；示例对第三方直接依赖固定版本。 |
| `settings` | 插件级设置字段列表。 |
| `actions` | 动作声明，包含 `key`、`title`、`parameters` 与以秒计的 `timeout`。 |
| `permissions` | 用途声明列表：`key`、`title`、`detail`、`sensitive`；不提供沙箱能力。 |
| `events` | 订阅类型列表，例如 `["recording.completed"]`，不是历史事件日志。 |
| `event_timeout` | 事件处理超时秒数，默认 120；范围 `(0, 3600]`。动作自己的 `timeout` 默认 30 秒，同样范围。 |

设置和动作参数使用同一种声明结构，但值的作用域不同：

```json
{
  "key": "mode",
  "title": "模式",
  "kind": "choice",
  "default": "local",
  "help": "只在本地处理",
  "options": [["local", "本地"]],
  "group": "处理",
  "required": true
}
```

`kind` 支持 `switch`、`choice`、`text`、`secret`、`number`。`number` 可声明 `minimum`、`maximum`；`choice` 的 `options` 是 `[值, 标签]` 二元数组。`visible_when` 可声明同一表单中依赖项，例如 `{"enabled": true}`。显示条件不是权限检查，处理器仍需验证输入。`group` 是展示分组，`help` 要说明目标站点、单位、敏感用途、限制和副作用。不要把平台专属逻辑或动态可执行表达式塞进声明。

宿主根据声明构造设置和参数表单。设置、绑定、来源持久化；密钥遵循凭据库规则。声明中的默认值不能代替处理器输入校验，尤其是整数、大小限制、有限数值和 URL。

## Python 入口与 Context

入口模块只需导出普通同步函数，不必导入 SDK，不要在顶层执行网络、键盘或录音副作用：

```python
def handle_action(context, action, event, parameters):
    if action != "example_action":
        raise ValueError("未知动作")
    context.log("开始处理")
    return {"ok": True}


def handle_event(context, event):
    if event.get("type") != "recording.completed":
        return {"ignored": True}
    # 只处理经过授权的完整保存结果；不要在这里启动下一次录音。
```

- `action` 是 manifest 中的动作 key；`parameters` 是当前手动运行或手势绑定的参数字典，与设置分离。
- `context.settings` 是本次任务的设置字典快照；不要修改后期待宿主同步保存。
- `context.data_dir`、`context.cache_dir` 是 `Path`。长期结果放 data，可重建的临时内容放 cache；按需创建目录，不将密钥写入任何文件。
- `context.log(message, level='info')` 写日志；不得记录 token、敏感正文或完整底层 HTTP 异常。
- `context.recording.start(max_seconds=60, live=False)` 返回包含 `state='starting'` 和宿主 UUID `session_id` 的字典；`stop(session_id)` 返回 `state='stopping'` 和会话标识，`status()` 查询宿主当前状态。它们均要求 manifest 声明 `recording.control`。
- `context.call(method, **params)` 是底层同步 RPC。它不是任意设备访问通道：宿主仍应检查状态、所有权、参数、互斥和授权。录音便利接口等价于 `recording.start`、`recording.stop`、`recording.status` RPC。
- RPC 只在同步处理器所在的线程及其有效调用期间使用；不要把 context 留给后台线程或定时器。返回 JSON 可序列化的小结果，不能返回 `Path`、字节或文件对象。
- 抛出有用且不含秘密的异常报告失败。宿主超时不自动重试，杀死进程也不能撤销已经发送的键盘、网络或设备副作用。不要用重试掩盖不确定状态。

### 事件含义

手势触发的 `event` 包含：

```python
{"type": "gesture", "gesture": "用户手势名", "timestamp": "2026-01-01T12:00:00+00:00", "confidence": None}
```

这是一次识别触发事件，**不是概率流**。`confidence` 可以为 `None`，未知就保留未知，不填成 0 或 1；即使有分值也不能擅自解释为校准后的概率。手动运行同样调用 `handle_action`，不要假定一定存在 `gesture` 或 `confidence`；示例动作不依赖这些字段。

完整保存事件包含：

```python
{
    "type": "recording.completed",
    "path": "/完整保存的文件.wav",
    "raw_path": "/原始数据文件",
    "duration_s": 2.0,
    "session_id": "宿主分配的会话标识",
    "owner": "example.recorder"
}
```

事件只代表宿主明确报告的状态，不靠时间推断、不靠“已停止”推断。事件订阅与动作绑定是不同通道；订阅默认自身、不回放、需用户启用，也可经授权选择所有新录音。手动选择已有 WAV 会收到 `manual=True, owner=None, session_id=None` 的完成事件，可能没有 `id`，示例仍会生成本地结果。`raw_only=True` 的完成事件只有完整 raw、尚未解码为 WAV，示例明确跳过。

## 已绑定模型的自动保存

- 新增绑定、把绑定迁移到当前模型、按同名手势复制绑定时，Studio 会保存完整模型快照。训练后尚未手动导出的模型也会保存。
- 快照位置：Studio 配置目录中的 `plugins/.models/<模型指纹>.gesture.json`。插件详情 → 手势绑定 → “自动保存的模型”显示完整路径。不同模型各自保留，不因清除某个插件的数据或卸载插件而删除。
- `plugins/.models/active.json` 记录最近使用的已绑定模型。启动时自动载入并验证指纹；不会自动连接设备、开启识别或开启手势控制，也不会覆盖本次已经手动加载的模型。恢复的模型作为独立导入模型使用，不随工作目录内训练数据变化而自动失效。
- 导入其他未绑定模型不会替换自动恢复目标；重新使用已有绑定的模型会更新恢复目标。缺失或损坏的快照只记录警告，不猜测加载其他模型。
- 此功能不能从旧绑定的指纹逆推出模型。升级前已有的绑定，需要手动导入对应模型一次以补存快照，以后无需重复导入。

## 调度、更新与数据

- 同一插件串行执行，跨插件可并行；最多 20 个等待任务，队列满拒绝新任务并记录。每条手势绑定默认冷却 800ms，成功入队后开始计时。关闭手势控制、换模型或停用绑定会取消尚未执行的手势任务，不能撤销已发生的副作用。
- 动作超时会终止插件及清空其队列，不自动重试或重启。录音设备仍受其最长时限和宿主停止按钮约束，不能将进程退出当成音频已保存。
- 手动更新先验证清单并准备依赖环境，再替换代码、环境及锁文件；准备失败保留旧版本。更新成功后插件停用，需重新确认。新 schema 若与现有绑定不兼容，更新会拒绝；先调整绑定再重试。
- 关闭应用会请求取消正在进行的依赖安装；依赖安装总时限为 300 秒。宿主使用环境的 Python 或 `python -m`，不承诺更新后 venv 内 console-script 的绝对 shebang 或激活脚本可重定位。
- UI 显示最近 100 条任务结果；日志写入插件自己的 `plugin.log`，约 1MiB 时轮转，保留一份旧日志。宿主遮挡已知密钥，但不能保证任意插件不会用其他方式输出敏感内容。
- 清除数据保留设置及绑定；卸载删除托管代码、环境、数据、设置、绑定及可删除的凭据，不删除原始来源目录。删除前必须确认。实际数据目录路径显示在插件详情页。

### 批量清理缓存与插件数据

在“设置 → 缓存与插件数据”中：

- **清理缓存**：删除所有已安装插件的 `cache/`、`plugin.log` 和轮转日志 `plugin.log.1`，保留 `data/`。
- **清除插件数据**：在上述范围之外永久清空 `data/`。请先备份需要保留的插件结果。

两项操作均需确认，保留插件代码、依赖环境、设置、密钥、绑定和共享模型，也不会删除采集数据或宿主录音。先在插件页停止插件服务，并等待录音和插件操作结束；清理在后台进行，期间不能启动插件服务。部分文件删除失败时会明确报错，已删除的文件不会恢复。

清理仅覆盖 Studio 管理的已安装插件目录，不扫描插件自行写入其他位置的文件，不删除安装暂存或共享模型。若要同时删除插件设置和凭据，请在插件详情中选择卸载。

## 开发与离线测试

1. 复制一个示例目录，修改稳定 ID、名称、权限说明和动作声明；若复制录音示例，也要同步修改代码中的 `PLUGIN_ID`。
2. 为每个参数设计限制，并在 Python 中复核。处理器导入阶段应没有副作用；不把外部输入交给 shell 或 `eval`。
3. 用 mock context 直接加载入口测试，再在隔离的低风险环境手动启用。HTTP 测试必须 mock opener，不能访问真实站点；键盘测试不能真实发送按键。
4. 测试取消、异常、超时、重复触发、文件丢失、拒绝授权、插件进程重启和宿主设备忙的路径。更新代码后重新确认信任，不静默迁移为更宽泛的权限。

本仓库新增测试：

```bash
python -m unittest discover -s tests -p 'test_plugin_examples.py' -v
```

这些测试仅使用标准库及宿主无界面模块：检查四个 manifest、真实 manifest 的 `recording.control` 权限、真实 SDK 的 RPC 参数及 starting/stopping 返回兼容、导入时不加载 `pynput`、键盘/媒体 mock、HTTPS 请求体与头构造、无重定向及错误脱敏，以及自身/所有新录音/手动 WAV 分析、raw_only 跳过和 session 清理边界。不联网、不操作真实键盘和设备，也不证明硬件兼容性。
