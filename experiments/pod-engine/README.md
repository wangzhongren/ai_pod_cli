# 正式 aipod pod 引擎生成测试

本实验直接从空目录执行正式 Python CLI，不使用手写引擎、模拟模型、手工组件注册或替代生成器。
需求见 requirements.md：2D 小引擎与可玩的打砖块演示，包含固定步长、输入、碰撞、暂停/重置、
无窗口运行、PNG、JSON 摘要和真实验证。

准备环境：当前 AIPod checkout 的 Python 安装、pygame-ce 2.5.8，使用已经配置的模型端点。
模型生成、阶段顺序、重试、修复及冻结由 Pod 自己处理。

实际运行方式：

```sh
aipod init
SDL_VIDEODRIVER=dummy SDL_AUDIODRIVER=dummy PYGAME_HIDE_SUPPORT_PROMPT=1 aipod pod --file requirements.md --yes
```

SDL dummy 仅用于构建/无窗口验证环境；可交互运行需正常显示环境。
本机续跑目录记录在未提交的 run-location.json；公开日志保存在 results/ 下。
尚未得到最终构建和运行结果前，不把规划或代码文件的存在视为引擎验证成功。

首次失败结果见 [RESULTS.md](RESULTS.md)，框架修复及正式续跑、独立行为验收见
[FRAMEWORK_FIX.md](FRAMEWORK_FIX.md)。原契约阻塞已修复；生成应用的 8 项行为检查为
5 项通过、3 项失败，尚未交付可用的完整引擎。

之后的沙盒与应用行为验收门禁修复见 [SANDBOX_FIX.md](SANDBOX_FIX.md)：
新验证器已在不修改引擎源码的情况下拦住这些错误。
