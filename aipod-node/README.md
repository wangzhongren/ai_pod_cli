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

所有层共用 `WorkspaceAgent` 和 `WorkspaceTools`，可选择列出、读取、搜索、增删改文件以及运行 shell。提示词将协议明确为 AIPod 原创的专用文本指令集（AIPod Instruction Set），采用 XML-like 表示语法。Agent 在普通响应正文中输出一条指令，由 AIPod 控制器解析执行并反馈结果。读写文件、shell、上游修改申请和 finish 都使用这一格式；源码和复杂命令放在 CDATA 中，对象使用嵌套标签，列表成员使用 `<item>`。

解析失败返回 `executed: false`、具体 `parse_error`（类别、原因及可定位时的行列和片段）、`format_help` 与 `format_example`。模型会收到针对 DSML 外壳、标签不匹配、CDATA 未闭合等错误的修正说明；损坏的指令不会被猜测执行。执行阶段的错误与解析错误分别反馈。

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
<request_change><target>providers</target>
<paths><item>src/providers/impl/storage/store.ts</item></paths>
<reason>已观察到的接口问题</reason><change>必要的修改及兼容要求</change>
</request_change>
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

Model、Provider、Service、Pipeline、Interface Agent 每次执行默认最多 100 轮指令；Pod Agent（含最终验收与共享文件修正）每次执行默认最多 200 轮。每轮一条指令，各自独立计数。每次 Pod 运行最多 10 次修改申请。部分文件会在失败后保留以便恢复。检查成功不代表需求已被完整覆盖。

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
