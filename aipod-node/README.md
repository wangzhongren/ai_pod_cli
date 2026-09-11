# AIPod Node

AIPod 的 Node.js / TypeScript 实现：**Agent 使用统一文件和 shell 工具开发，Pod 协调跨层修改和最终验收。**

整体设计见 [项目 README](../README.md)。本文对应 GitHub `main`；本次工作区 Agent 重构尚未重新发布到 npm，下面优先给出源码运行方式。

## 从源码运行

需要 **Node.js 20+**。Agent shell 在 macOS 使用 `sandbox-exec`，Linux 需要 `bubblewrap`；原生 Windows 暂不支持这一执行方式。

在仓库的 `aipod-node/` 目录执行：

```bash
npm ci
npm run build
npm run cli -- help
npm run cli -- init ../../demo-node
```

配置一个实际可用的 OpenAI-compatible 模型：

```bash
export OPENAI_API_KEY="your-api-key"
export OPENAI_BASE_URL="https://api.openai.com/v1"
export OPENAI_MODEL="your-model"

npm run cli -- pod "开发问候服务，接受 name，提供路由和 CLI 入口。" --project-root ../../demo-node
```

使用已发布的 npm 版本时，可以安装为项目依赖或全局 CLI：

```bash
npm install --save-dev aipod-node
npx aipod-node help

# 或者
npm install --global aipod-node
aipod-node help
```

发行包可能落后于 `main`。需要本文的新流程时，请使用源码构建。

## Agent 如何协作

```text
Model → Provider → Service → Pipeline → Interface
                      ↑
             Pod 调度、审批与验收
```

所有层共用 `WorkspaceAgent` 和 `WorkspaceTools`。默认 `translated` 模式中，工作 Agent 在 `<need_function_tool>需求</need_function_tool>` 内描述一个操作，转换 Agent 将需求转成现有本地操作的结构化参数，控制器校验后执行。列出、读取、搜索、增删改文件、shell、上游修改申请和完成交接均走这一路径。转换 Agent 不获得额外文件权限，源码必须由工作 Agent 完整提供并逐字保留。Python 端使用相同默认流程。

SDK 参考集中在 [sdk-reference.ts](src/agent/sdk-reference.ts)，随包发布，并按当前层注入系统提示词。它包含准确的依赖注入方式、契约、调用签名、返回结构及最小示例；Interface/Pod 还会收到长驻入口的启动检查说明。历史对话裁剪后参考仍然保留，Agent 优先使用它，缺少具体细节时再查源码。其他章节也可通过 `import { sdkReference } from "aipod-node"` 后调用 `sdkReference("interfaces")` 等方式取得。回归测试会把提示词中的原样示例组成临时项目，完成 TypeScript 检查及真实 SDK 执行。

需求格式或转换失败会返回 `executed: false`，工作 Agent 修正后重试。显式 `direct` 模式仍支持直接输出 AIPod 指令；完整解析诊断保留给调用方，模型历史只接收错误类别、位置与合法示例，不回放错误回复原文。执行阶段的错误与格式错误分别反馈。

工作历史达到 **160,000 字符**时会调用当前模型压缩，保留摘要与最近几轮原始结果；历史硬上限为 **320,000 字符**。这里按历史和摘要的 JSON 字符数计量，不是 token 数，需求与 SDK 系统提示词单独固定保留。摘要不授予权限，也不能充当检查通过的证据。压缩前原始历史写入 Agent 临时目录；失败时先保留并重试，到达硬上限仍无法压缩则停止，不再静默丢弃旧记录。Python 端采用同一策略。

| Owner | 可写范围 |
|---|---|
| Model | `src/models/`、`tests/models/`、`docs/models/` |
| Provider | `src/providers/`、`tests/providers/`、`docs/providers/` |
| Service | `src/services/`、`tests/services/`、`docs/services/` |
| Pipeline | `src/pipelines/`、`tests/pipelines/`、`docs/pipelines/` |
| Interface | `src/interfaces/`、`interfaces/`、`tests/interfaces/`、`docs/interfaces/` |
| Pod | 共享 package/config 文件、`node_modules/`、`tests/pod/`、`docs/pod/` |

其他层的文件保持只读，注册表与 Pod 状态由控制器管理。发现上游问题时，Agent 发出申请：

```xml
<need_function_tool>申请由 providers 修改 src/providers/impl/storage/store.ts：现有存储接口不能读取指定条目，需要补充读取方法并保留已有调用兼容性。</need_function_tool>
```

Pod 批准后交给原 Owner 修改，重新检查受影响的中间层，再让申请者继续。申请者不会获得上游写权限。决定与结果保存在 `.aipod/plan.json`，已批准但中断的工作可以续接。

默认流程不再逐组件生成冻结测试或复制项目。Agent 选择普通编译、测试和运行命令，Pod 使用同一套工具验收最终交付。

### 三个目录，内部由 AI 规划

`src/providers/` 与 `src/services/` 都包含三个固定区域：

```text
src/services/
├── contracts/physics.ts
├── impl/physics/
│   ├── collision/
│   └── dynamics/
└── public/physics.ts
```

`contracts/` 定义稳定接口、类型和行为规则，复用 Model；`impl/` 放具体实现及内部辅助代码；`public/` 只做显式命名导出，例如：

```ts
export {CollisionService} from '../impl/physics/collision/service.js';
```

注册表中的 `file` 指向 `src/services/public/physics.ts`，`id` 为 `CollisionService`。支持别名、逐层命名导出以及一个入口公开多个组件。校验沿导出找到真实类，内部文件不必逐个注册。组件入口不使用 `export *` 或动态赋值。

AI 自行决定三个区域下的目录、名称和深度；无需镜像目录或为每个函数创建接口。跨层只能导入公开入口或契约，不能引用另一层的 `impl/`，契约也不能反向依赖实现。内部辅助代码可以复用，Service 之间的编排继续放在 Pipeline。

新生成组件使用公开入口，既有平铺注册继续兼容。原 Owner 管理整个层的目录，跨层修改仍需 Pod 批准。一个公开文件里删除某个组件时，只移除对应导出和注册信息，保留其他导出。

## 运行时边界

- **Model**：共享数据类型。
- **Provider**：基础能力，通过依赖容器提供给 Service。
- **Service**：实现 `execute(context)`，使用自己的契约、Model 和 Provider。
- **Pipeline**：组合 Service，管理顺序、并行、重复及流式执行。
- **Interface**：通过注册路由提供用户入口。

Service 不直接导入、注入或调用其他 Service。组件、路由和 Interface 记录在 `aipod.json`。

声明输入输出后，可以使用有类型的 Context：

```ts
import { PipelineContext } from "aipod-node";

export class PriceTotal {
  execute(context: PipelineContext) {
    const ctx = context.typed(
      { price: { type: "number" }, quantity: { type: "integer" } },
      { total: { type: "number" } },
    );
    return ctx.output({ total: ctx.get("price") * ctx.get("quantity") });
  }
}
```

TypeScript 检查会解析项目内相对导入和运行时声明，发现跨文件类型、导出和 API 使用问题。内置 `ModelRepository` 使用 JSON 存储，不等同于 Python 的 SQLModel 实现。

## 常用命令

以下命令假设已安装包；源码开发时可把 `npx aipod-node` 换成 `npm run cli --`。

```bash
npx aipod-node inspect ./demo
npx aipod-node interface list --project-root ./demo

npx aipod-node pod "调整 CLI 展示，保留领域行为。" --stage auto --project-root ./demo
npx aipod-node create --category service --description "格式化问候内容" --project-root ./demo
npx aipod-node compose "先验证输入，再生成问候" --project-root ./demo
```

需求转换已是默认模式，目前需使用源码版本：

```bash
npx aipod-node pod "调整定价规则，保留其他行为。" --stage auto --project-root ./demo
```

默认 `translated` 模式中，工作 Agent
只输出一条 `<need_function_tool>具体需求</need_function_tool>`；专门的转换 Agent 使用
独立模型请求将它转换为本地操作参数，原控制器再执行文件归属、权限和注册校验。
转换 Agent 不执行命令、不拥有额外写权限；源码必须由工作 Agent 完整提供，转换结果须逐字匹配。
缺失参数或转换失败会反馈给工作 Agent，不能静默执行猜测出的源码。

两阶段使用同一个已配置模型。`pod`、`create`、`compose` 和 Studio 均默认启用，无需参数。
如需旧的直接指令方式，三个 CLI 命令均可显式指定 `--instruction-mode direct`；恢复时继续传入该参数。

使用实际生成的路由与 Interface 名称运行：

```bash
npx aipod-node run greet --params '{"name":"Ada"}' --project-root ./demo
npx aipod-node interface run GreetingCli --payload '{"name":"Ada"}' --project-root ./demo
```

自动修改时，Pod 可识别最早受影响层及既有目标，再计算依赖范围。局部更新保留目标 ID 和授权路径；需要额外修改时走 Pod 申请。新增、删除、重命名或无法确定范围的变化使用整层处理。

## 检查与临时运行

```bash
npx aipod-node verify ./demo
npx aipod-node verify ./demo -- node --test
```

只做结构检查时结果为 `unverified`；第二条还会运行指定命令。请按项目实际情况选择测试命令。

已有冻结组件测试是可选工具：

```bash
npx aipod-node verify ./demo --component-tests
```

也可显式使用 `defineComponentTests`、`TestSandbox` 和 `verifyComponentTests`。这些驱动的测试完整性规则继续有效，但普通构建不以它们为前置条件。

Agent shell：

- 在同一项目中工作，由系统强制限制可写范围。
- 使用 `.aipod/work/<owner>/` 存放临时数据、缓存和编译结果，不继承模型 API 凭据。
- 通过 `AIPOD_BUILD_DIR`、`AIPOD_DATA_DIR` 指定临时输出；自定义外部系统仍需测试配置。
- 通过 `AIPOD_NODE_CLI`、`AIPOD_NODE_MODULE` 找到当前运行时。
- 默认 60 秒，单次最多 120 秒，输出长度受限。

Linux 下新的共享根文件应先由文件工具创建，再交给 shell 修改。缺少权限后端时会报错，不会退回无约束执行。

Model、Provider、Service、Pipeline、Interface Agent 每次执行默认最多 100 轮需求；Pod Agent（含最终验收与共享文件修正）每次执行默认最多 200 轮。每轮一条需求，各自独立计数；转换请求及内部重试不等于新工作轮次。每次 Pod 运行最多 10 次修改申请。部分文件会在失败后保留以便恢复。检查成功不代表需求已被完整覆盖。

## 配置与 Studio

Python 和 Node 共享 `~/.aipod/config.toml` 的 `[env]` 配置，优先级为：

```text
进程环境变量 → 项目 .env → 全局 [env]
```

`ConfigStore` 支持项目 `config.toml` 的点号键访问，并兼容 Node 的 `config.json`。

```bash
npx aipod-node config set OPENAI_MODEL your-model
npx aipod-node studio ./demo
```

Studio 监听本机地址并使用进程级令牌，提供项目、源码、路由、运行记录和 Pod 状态查看。

## 更多能力

运行时还提供结构化 Success/Failure、顺序与并行组合、受限重复、异步流及 Broker/Worker。分布式消息交付为 **at least once**，业务侧仍需幂等处理；当前 Broker 不提供高可用复制。

参见 [distributed-orders 示例](examples/distributed-orders/README.md) 和 [Agent 使用说明](SKILL.md)。

## 开发

```bash
npm ci
npm run check
npm test
```

需要本机端口或系统权限的测试应在允许这些操作的环境运行。

[MIT](LICENSE)
