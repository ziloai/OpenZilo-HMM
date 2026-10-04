# Publication checklist / 发布检查清单

## English

Initial private import review for [ziloai/OpenZilo-HMM](https://github.com/ziloai/OpenZilo-HMM). Keep the repository **private** until the maintainer explicitly authorizes a public release. Re-run the relevant checks for future releases.

1. [x] **License:** root [LICENSE](../LICENSE) contains the same unmodified MPL-2.0 text as OpenZilo. Python source files carry SPDX notices. This repository contains no hardware designs, so the separate upstream hardware license is not needed.
2. [x] **Redistribution authorization:** the OpenZilo maintainer has confirmed use and redistribution of the supplied example datasets, models, and imagery. Scope and provenance are recorded in [NOTICE.md](../NOTICE.md).
3. [x] **Repository and support:** the final repository is [ziloai/OpenZilo-HMM](https://github.com/ziloai/OpenZilo-HMM), created as private. Both READMEs use its clone URL and [Issues](https://github.com/ziloai/OpenZilo-HMM/issues) for feedback; access requires repository permission while private.
4. [x] **File audit:** reviewed the 30 staged files. `detect-secrets` 1.5.0 (network verification disabled) reported zero findings. Additional checks found no personal filesystem paths, email addresses, or non-placeholder BLE addresses in text and pickle metadata; the PNG has no text/EXIF metadata. Datasets contain only gesture metadata and six-axis readings. Local environments, caches, OS files, and generated recordings/models are ignored. Commit author email uses GitHub's noreply address. This is a scoped review, not a guarantee that every possible secret pattern can be detected.
5. [x] **Software checks:** dependency installation and all 23 tests passed on Python 3.10 and 3.12. Tests cover the six included models and 30 example repetitions, CSV import, re-training, the offline CLI, and simulated SDK lifecycle/error handling.
6. [x] **Hardware acceptance:** the OpenZilo maintainer confirmed this item is acceptable for the initial private import. The coding agent did not run real BLE tests, and this record does not claim a tested matrix of kit/firmware combinations. Revalidate affected hardware and firmware before subsequent releases.
7. [x] **Documentation:** checked both READMEs, matching command/JSON examples, local links, final repository URLs, the SDK commit pin, model/data inventory, and the upstream license/image copies. License and authorization placeholders have been removed.
8. [ ] **Held-out accuracy evaluation — deferred by the maintainer.** No independent accuracy claim is made. Training-set predictions and the confidence heuristic are not a real-world accuracy benchmark.

## 中文

这是 [ziloai/OpenZilo-HMM](https://github.com/ziloai/OpenZilo-HMM) 首次私有入库的核对记录。维护者明确授权公开发布前，仓库保持 **private**。后续发布需要重新执行相关检查。

1. [x] **许可证：** 根目录 [LICENSE](../LICENSE) 与 OpenZilo 使用同一份未经修改的 MPL-2.0 正文，Python 源文件已添加 SPDX 声明。本仓库不包含硬件设计，无需采用上游单独的硬件许可证。
2. [x] **再分发授权：** OpenZilo 官方维护者已确认所附示例数据、模型和图片可以使用及再分发。许可范围与来源记录于 [NOTICE.md](../NOTICE.md)。
3. [x] **仓库与反馈渠道：** 最终地址为 [ziloai/OpenZilo-HMM](https://github.com/ziloai/OpenZilo-HMM)，已按 private 创建。两版 README 均使用该仓库的克隆地址和 [Issues](https://github.com/ziloai/OpenZilo-HMM/issues)；私有期间访问需要仓库权限。
4. [x] **文件检查：** 已核对 30 个暂存文件。`detect-secrets` 1.5.0 在禁用联网验证的情况下扫描结果为零；补充检查未在文本和 pickle 元信息中发现私人目录、邮箱或非占位符 BLE 地址，PNG 不含文本/EXIF 元信息。数据集仅包含手势元信息和六轴数值。本地环境、缓存、系统文件和生成的录制数据/模型已忽略，提交作者邮箱使用 GitHub noreply 地址。这是限定范围内的检查，不代表能检测所有可能的敏感信息。
5. [x] **软件验证：** 已完成依赖安装，23 项测试在 Python 3.10、3.12 均通过。覆盖六个自带模型、30 次示例录制、CSV 导入、重新训练、离线 CLI，以及模拟 SDK 生命周期和异常处理。
6. [x] **硬件验收：** OpenZilo 官方维护者确认该项不阻碍首次私有入库。编码助手未执行真实 BLE 测试，本记录不声称已覆盖某个套件/固件测试矩阵；后续发布前应重新验证受影响的硬件和固件。
7. [x] **文档核对：** 已检查中英文 README、对应的命令和 JSON 示例、本地链接、最终仓库地址、SDK 固定提交、模型/数据文件清单，以及上游许可证和图片副本。许可证及素材授权的待确认说明已移除。
8. [ ] **独立测试集准确率评测——按维护者要求暂缓。** 暂不声明独立评测准确率，训练样本上的预测和经验置信度不作为真实场景准确率。
