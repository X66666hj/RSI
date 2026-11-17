# RSI · Terminal-Bench-Evo 扩题与 Skill 学习 Pipeline

本仓库提供当前版本的错误定位、执行对照、EnvHarness 扩题、变体评分和 A/B/C Skill 学习流程。核心入口位于 pipeline/，底层环境执行器位于 matts_official/，公共文件锁和提交归档位于 skill_abc/。

## 安装与环境

任务执行需要 Linux、Python 3.12、Bash、GNU coreutils、chroot 所需权限及 libseccomp.so.2。当前后端使用 native rootfs，各题需要通过环境验收。涉及特定操作系统、网络服务或容器行为的任务应单独核对环境差异。

~~~bash
python3.12 -m venv venv
venv/bin/python -m pip install -r requirements.txt
venv/bin/python -m pip install -e pipeline/vendor/envharness-main
export PYTHONPATH="$PWD/pipeline:$PWD/pipeline/frozen-runtime:$PWD/skill_abc:$PWD/pipeline/vendor/envharness-main"
venv/bin/python -m unittest discover -s pipeline -p 'test_*.py'
~~~

在主机设置 TYPESAFE_API_KEY，将 Qwen key 保存到 pipeline/.qwen.key 并设置权限 600。模型角色使用 qwen3.8-flash 开启 thinking 解题，qwen3.8-max 出题和优化，Jev 决定组件及 Skill 规则。录制器按类型过滤 reasoning 正文，保留工具调用、结果、事件编号和用量。

完整运行需要部署方提供数据集任务、验收后的 rootfs profiles、干净公开初态归档和路由回执。bootstrap.sh 接受仓库根目录的 ubuntu-base.tar.gz 并安装基础系统依赖，各题的额外依赖按任务环境准备。应在专用 Linux 执行主机运行 bootstrap.sh。

pipeline/frozen-runtime/ 保存当前控制器依赖的固定执行模块，recorder-lock.json 校验随包录制器源码。PROVENANCE.json 记录本次包的文件指纹，第三方来源见 THIRD_PARTY_NOTICES.md。

## 执行顺序

从允许使用的任务数据准备完整 EVO 题族，在 pipeline/baseline-plan.json 的 train_routes 中登记每个版本的源文件目录、环境 profile、初态归档、SHA-256 和准入回执。路由字段以 envharness_full_coverage.verify_route() 为准。examples/train-plan.example.json 展示计划结构，实际路径、文件指纹和 Oracle/NOP 回执由部署环境产生。

~~~bash
cd pipeline
../venv/bin/python envharness_abc_run.py runs/baseline --baseline-plan baseline-plan.json
../venv/bin/python envharness_abc_run.py runs/baseline --diagnose-baseline
../venv/bin/python envharness_deepseek_expand.py design-request.json runs/designer
~~~

先核对实际失分项，再截取必要上下文复核 Top 3 候选归因，执行最小对照。确认的方向分别写出题简报，格式见 examples/design-request.example.json。出题器从 baseline-plan.json 或 RSI_TRAIN_PLAN 环境变量指定的计划读取 Train 路由。

Setup、Rules、Link 候选都需要独立重放。任务相关的评分对照由控制器执行，在同一变体输入上运行 Oracle、NOP、错误实现和修复实现，期望分数为 1/0/0/1。将验收后的 route 及证据写入 train-plan.json 的 variants 和 confirmed_replays。

~~~bash
../venv/bin/python envharness_abc_run.py runs/abc --plan train-plan.json
../venv/bin/python envharness_abc_run.py runs/abc --retest-train
../venv/bin/python envharness_abc_run.py runs/abc --test-plan heldout-routes.json
~~~

训练完成后执行复测。--retest-train 固定 A 的 S0 与 B/C 的 S4，分别求解原题的全部 EVO 阶段。--test-plan 接受按任务 ID 索引的留出路由。每道 C 变体配一次 B 同版父题练习，训练总次数一致。

部署中将原执行器绝对路径替换为仓库相对位置，统一设计路由输入和录制器文件校验，并将优化器的模型调用绑定到当前 Qwen 路由。运行前按下文约定执行一条真实 thinking 与轨迹归档检查。跨阶段的任务环境按作者版本加载，Skill 在训练阶段连续继承。

## 全局实验协议

本文件统一说明全局实验设计、方法、角色分工与验收要求。具体题目的问题分析、候选数据、运行进度和结果保存在对应实验记录中。

## 目标与总体流程

验证经过因果核查的定向扩题，是否比等次数重复父题更有助于 Skill 学习和留出任务表现。

1. **先跑 EVL。** 用已验收的原题评分确认 Train 失败，复核原轨迹中与失分相关的事件。
2. **定位并复现错误。** 从原轨迹截取必要上下文，在独立 Solver 会话中对照验证。
3. **生成并验收扩题。** 按已验证的错误机制选择 EnvHarness 组件。核对公开题意与评分条件，在干净环境通过评分对照后登记。
4. **纳入 Train。** 凑齐完整 EVO 链后运行 A/B/C，B 对同一 EVO 父题的重复次数和 C 练习变体的次数一致。留出评测单独进行。

只对存在失败轨迹、且目标错误机制有执行证据支持的 Train 父版本扩题。最终评分为 0 本身不足以确认 agent 错误；基线全量运行不意味着全量扩题。Test 不参与定位、生成、诊断设计或 Skill 更新。

变体应暴露已定位的错误机制，并体现可复用的能力挑战。优先选择足以暴露该机制的最小环境改动。仅修语法、改名、增加提醒或占位干扰不构成有效扩题方向。

## 角色与模型分工

| 角色 | 新协议默认模型 | 职责 |
|---|---|---|
| Solver | qwen3.8-flash，开启 thinking | 原题、变体及最终评测的解题 rollout |
| 证据提取 | qwen3.8-flash | 整理事件、引用与环境信息 |
| 进化器／优化器 | qwen3.8-max | 提出原因假设、依据验证修订归因、形成候选 Skill 修改 |
| 组件与 Skill 决策 | Jev `jev-1.14.0` | 在重放尚未验证组件时择一；逐条判断候选 Skill 规则是否采纳，并选择 `task` 或 `format` |
| EnvRigger 出题 Agent | qwen3.8-max | 读取真实公开环境，在选定组件内生成候选并执行自检 |
| 独立审查 Agent | qwen3.8-max | 审核证据、竞争解释、扩题价值及有分歧的判断 |

上表定义新协议的默认设置。已冻结批次的实际模型路由见[角色配置](pipeline/envharness_qwen_roles.json)。DeepSeek Harness 是执行框架，模型是独立配置。不同角色使用独立会话；同一模型的独立会话不等于没有共同判断偏差，最终仍需执行证据。

模型、thinking、提示、初始 Skill、预算与工具版本在实验前冻结。任何执行策略变更都须记录协议版本，并对 A/B/C 一致应用；不同版本的基线不能直接混算。

thinking 开关对照使用隔离诊断目录，固定题目初态、Skill、提示与预算，验证实际请求开关及推理内容是否落盘。分别统计正常完成与正确率、执行步数、工具重复调用、推理内容量、token 和耗时；精确重复调用不等于语义上的无效验证。历史轨迹对照须标明非同期和单样本限制。此类诊断不增加 B/C 训练次数、Skill 更新或候选准入，正式实验配置仍保持冻结。

新协议的 Solver 开启 thinking，运行中的 Harness 会话使用临时内存目录。在写入 `events.jsonl` 时按类型移除 `reasoning` 内容块、流式推理片段及对应回放元数据，保留原事件编号、工具命令和结果、普通回复、结束状态与用量统计。录制回执保存收到的推理块数量，以及事件编号序列和工具事件的校验值。历史轨迹通过[分析轨迹入口](pipeline/envharness_analysis_trace.py)应用同一过滤规则。此设置减少分析输入量，模型推理生成量由运行协议中的 thinking 参数控制。若另做推理预算对照，单独记录每次请求的 `thinking_budget`，按相同协议比较正常完成率、步数和耗时。

采集诊断轨迹前，先用一条 Train 原题的真实会话验证 thinking、事件编号、工具调用和结果的归档，再启动同协议的采集队列。将正常通过、正常结束但评分失败和执行中断分别记录，只有经证据复核的错误才可用于扩题归因。诊断样本的 B/C 正式训练计数为零。

## 错误定位与模型对照

以完整题族作为运行和判断修复能力的单位。按 EVO‑1→4 核对各阶段原题结果，保留已通过阶段作为回归对照。局部诊断定位具体错误，回到整条链检查修复是否能在后续阶段复用、是否引入回归。逐阶段记录目标错误是否重犯、原题评分及其他失败项。

每轮逐一核对冻结 Train 清单中的全部父版本。对存在有效失败轨迹的父版本按下述规则检查候选归因，可直接复用同一失败机制的可信执行对照。对未发现有效失败或无法确认机制的父版本记录结论。归因覆盖按父版本汇总，扩题数量按实际通过验收的变体汇总。

1. **从实际失败项提出候选。** 核对运行结束状态、实际提交和评分失败项。反思器对每个失败项先尝试提出三条不同、可分别验证的归因，按证据强弱排序，记录相关动作、工具结果、事件编号和最小对照。证据不足三条时记录缺口，保留有依据的候选。Top 3 统计的是对失败原因的候选解释；成功观察、引用定位成功以及已经恢复的中途错误不能充当已确认的失败归因。
2. **逐条复核。** 先排除与记录直接矛盾的候选。优先复用有效的历史重放；需要补证时，固定输入和其他条件，只改变被怀疑的因素，检查目标失败项是否修复，并检查原来通过的项是否回归。需要验证 Solver 是否重犯时，将相关轨迹片段和公开任务约束交给独立 Solver，沿用原模型、thinking 配置和 Skill。
3. **记录判定并分流。** 每条归因记录为确认、排除或未确认（`unresolved`）。只有执行对照支持同一因果机制时才合并归因，并记录合并依据。暂时无法区分的方向分别保留及复核。每个经执行对照确认且有可复用解题价值的方向单独编写出题简报，可设计多个有实质差异的场景，逐道验收并登记。父版本无可扩机制时照常完成原题 EVO 链。每个失败项最多复核三条候选，每个父版本最多准入四道。归因数量、候选数量、准入数量与正式训练数量分别记录。

局部 Solver 对照的结论限定在所给上下文和输入。候选归因引用可定位的事件，只能证明对应观察真实；修复型 Skill 还需核对该机制与实际失败项的关系。扩题安排逐条对应归因及复核结论，不按 Top 3 数字凑题；多个有效修复不自动代表多个独立原因，归因合并须说明执行证据。同一原因的不同场景分别记录为变体。诊断和重放单独记录，B/C 训练计数为零。

扩题优先验证当前 Solver 仍会反复犯的错误。独立 Solver 的复现率用于排队排序，有执行证据确认的错误进入出题流程。

Solver、EnvRigger 和其他有工具的 Agent 使用独立的离线任务工作区。工作区只放当前任务允许的公开材料与本次需要的轨迹片段。开跑前验证网络访问受阻，检查工作区没有可追溯到题解或旧轨迹的 Git 历史、历史实验目录、隐藏测试和参考解。用到 Git 的任务仅提供任务所需的仓库状态。

## 扩题生成与验收

设计器通过 DeepSeek Harness 的真实工具会话读取公开题意、环境、历史证据和已验证假设，根据错误出现的条件选择改造组件。

| 错误的触发条件 | 优先选择 | 独立重放检查 |
|---|---|---|
| 更换初始数据或环境即可暴露问题 | **Stage / `Setup`**，在 Solver 开始前重放操作以改变初态 | 核对允许修改的文件、初态差异与任务可解性 |
| 特定工具调用、返回或观察会暴露问题 | **Contract / `Rules`**，在交互过程中调整动作、返回或观察 | 核对公开交互约束、规则生效的条件与合法解法是否仍可执行 |
| 多个子任务连续执行时才暴露问题 | **Chain / `Link`**，在同一次 rollout 中组合子任务 | 核对子任务顺序、状态传递与各子任务评分 |

出题简报提供错误证据和历史实现 0／修复实现 1 的结果。已通过执行重放验证组件时，在简报记录 `replayed_component`，控制器直接沿用该组件。归因已确认但组件尚未验证时，控制器才让 Jev 在可用组件中择一，并保存原始回执。验证阶段每个候选只用一个组件；只有存在已验收的同题族后续 Train 任务时才提供 `Link` 选项。出题 Agent 在选定组件内设计改动，控制器重放验证，再冻结环境、题意和评分规则用于 B/C 对照。

出题流程读取已复现的错误，要求记录触发条件、重放证据及错误实现 0 / 修复实现 1。DeepSeek Harness 出题 Agent 先用 Bash 查看真实公开环境，再用 `propose` 提交候选。控制器核对候选使用的组件是否与选定组件一致，并在独立环境执行公开自检。三种组件共用一份候选结构和原题环境物化流程。

`Rules` 当前支持 Bash 返回分页，并向 Solver 公开续页工具。每次 Bash 调用从当前任务的初始工作目录启动，文件改动在该段内保留。`Link` 在一次 rollout 内连接两个已验收的 Train 任务，第一次提交时进入下一段，可传公开交接摘要，每段使用独立文件系统和原评分器。两段均通过时总分为 1，任一段失败为 0，评分器或环境异常保留为未评分。EVO‑1→4 仍定义跨版本的 Skill 推进顺序。

现有 Solver 启动器读取已通过自检的候选，校验出题记录和候选指纹，复用对应原题基线的 Skill 和预算。会话通过 DeepSeek Harness 调用公开的观察、Bash、续页和提交工具，控制器独立记录评分。两段均提交才构成完整的 Link Solver 轨迹。离线对照复用原题基线与历史提交，验证 Oracle/NOP 和错误实现 / 最小修复是否分别得到 1/0 与 0/1。组件对照用于检验执行和评分，正式训练仍由完整 EVO 链与 B/C 配对计划准入。

```bash
designer_session=$(python envharness_deepseek_expand.py design-request.json design-output)
python envharness_qwen_agent.py solver-output --designer "$designer_session" \
  --baseline "$original_baseline"
```

设计器执行自检，控制器在另一干净副本中独立重放。隐藏 tests、Oracle 解法、凭据和 Test 材料仅由控制器管理。历史实现、最小修复及参考输出只用于隔离诊断与评估。保留父题公开目标、底层工具链和原评估器，组合任务按各子任务的公开目标评分。

### 单题准入

1. **对齐题面与评分。** 将会影响分数的任务要求写入 Solver 可见的公开题面，核对实际环境改动和评分条件。
2. **验证评估器。** 在干净副本上执行最终用于 C 的评分。Oracle/NOP 应分别得到 **1/0**。在同一扩题输入下保持其他条件一致，目标错误实现及其修复应分别得到 **0/1**，并能从评分明细确认目标错误造成了失分。同版候选和评分合同的已有对照可复用。可信正确实现同时满足两项正对照要求时，可复用其结果。
3. **冻结并登记。** 在准入回执中绑定父版本、确认的归因及重放证据，并冻结候选、题面、输入和评分器。按父版本和变体身份去重，同一已确认机制可关联多道有实质差异且通过验收的变体，每道进入 C 的变体配一次对应的 B 父题练习。

开发轨迹及原题和变体的 3+3 对照用于按需观察 Solver 行为。独立审题、EnvHarness `decide` 和轨迹行为复核归入出题诊断。单题准入依据上面的执行对照，训练收益由 A/B/C 衡量。

每个父版本可有 0–4 道合格变体，最多尝试 4 个候选槽，每个候选最多修订一次。扩题以复现的错误和改动价值为依据。诊断题转为训练题时，需另建身份、走完整准入并计入上限。

## LoopTool 与 EnvHarness 的分工

| 方法或组件 | 在实验中的作用 |
|---|---|
| EnvHarness 组件 | 根据已复现的错误选择改初态、改交互或组合任务 |
| LoopTool EDDE 思路 | 围绕核实的失败机制扩充情境，保留核心挑战 |
| LoopTool 规则检查 | 校验动作名称、参数覆盖和类型，命令执行及业务有效性由独立重放验证 |
| LoopTool JGLV 适配 | 当新旧审题结论冲突时，比较两份判断，允许纠错或保持未决 |
| 项目执行验收 | 负责环境重放、参考解、错误实现对照、实际提交评分和轨迹完整性 |

[LoopTool 适配器](pipeline/looptool_terminal_adapter.py)复用固定指纹的官方校验函数及比较提示。本项目比较的是审题判断，采用 Skill 更新；不等同于完整复现论文的 GCP/PPL 筛选、原始 EDDE 管线或 GRPO 权重训练。模型对照是本项目的错误诊断流程。

审题接受、题目可解、评估器有效、当前模型有学习需求和训练后迁移提升，是不同结论，分别取证。

## EVL 与变体评估器

原 benchmark 评估器保持冻结。按组件改动定义对应的变体检查，覆盖初态差异、交互条件或子任务结果。核对评估时的重置行为，确保新增条件确实被评测覆盖。

| 检查 | 目的 |
|---|---|
| Oracle | 可信正确解应通过，证明任务与评估条件相容 |
| NOP | 不执行解题操作应因任务断言失败，排除空测试或无条件通过 |
| 错误实现与针对性修复 | 验证评估器能观察目标机制，避免缺陷被其他条件掩盖 |
| 实际 Solver 提交 | 评分对象必须是归档的真实产物，不能用诊断补丁替代 |

Oracle/NOP 不等于错误机制已经复现。对照应依据任务语义设计，不能要求所有任务套用固定数量或固定类型的缺陷检查。

输入、初态、参考实现、评分代码和聚合规则绑定在冻结的评估合同中。每例从干净状态重放；预期结果由独立可信环境产生，不向 Solver 泄露。代码、合同或用例变更必须建立新版本，不能根据 Solver 的结果事后挑选用例。

分别记录原题分数、变体分数、环境有效性、评分完整性与执行完整性。保留原检查的 C 变体以新旧检查均通过判定产物成功；异常或缺失结果标为未评分，轨迹超步数不会因产物通过而变成正常完成。

## A/B/C 与完整 EVO 链

按任务家族划分 Train/Test，防止同一家族的相近版本跨集合泄漏。以完整 EVO-1→4 链训练和评测；具体选题、规模、预算及调度写入冻结的运行配置。

探索阶段可将本批所有家族作为开发数据，用于收集失败、检查归因和验证扩题方法。单独记录这类运行和旧评测结果的复用范围。恢复正式 A/B/C 前，重新冻结从未用于归因、扩题或 Skill 更新的留出家族。探索阶段分数仅用于开发判断。

| 组 | 训练与 Skill | 最终 Test |
|---|---|---|
| A | 初始 S0 baseline，不更新 | 使用 S0 评测完整链 |
| B | 按题族独立训练，每阶段解原题并按 C 的变体数重复对应父题 | 使用每个训练题族的 B 组 S4 评测完整链 |
| C | 按题族独立训练，每阶段解原题及对应合格变体 | 使用每个训练题族的 C 组 S4 评测完整链 |

每个 Train 题族从同一初始 S0 分别启动 B/C 两条链，各自按 `S0 → EVO-1 → S1 → EVO-2 → S2 → EVO-3 → S3 → EVO-4 → S4` 推进。每阶段使用本组本题族的阶段前 Skill，练习完成后仅用本组本题族在该阶段的轨迹更新 Skill，下一阶段继承更新结果。环境逐次重置，各题族可并行。身份完全相同的完整 S0 原题记录可复用为 A 的基线和 B/C 的首阶段原题练习。

**C 每练一道合格变体，B 就重做一次同一 EVO 父题。** 没有合格变体的阶段，两组各练一次原题并继续更新 Skill。各阶段的变体数可以不同，两组正式训练题目总数相同。冻结这份配对计划并完成基线验收后，再进入正式 B/C。

采用单组件验证协议时，冻结前逐题核对组件的执行证据、候选实际使用的组件和验收回执。已有变体通过同一错误的 0／1 对照，且题面、输入和评分合同通过身份核对时，可直接复用原验收和匹配的 Solver 轨迹。新变体经过对照验收后加入配对计划。组件决策与验收单独记录，训练次数只计正式配对练习和原题练习。

B 仅用原题评估器，C 的原题练习也用原评估器，只有 C 扩题追加变体评估。最终按训练题族报告 `A(S0) / B(该题族 S4) / C(该题族 S4)` 的留出对照。所有对照使用相同留出任务、完整链和原评估器。A 的同配置留出结果供各题族共同对照。留出测试使用冻结 Skill，不更新或利用 Test 反馈。

若要测已见错误的修复，训练完成后用 A 的 S0、B/C 各自题族的 S4，在该题族完整原题链上复测。三组使用同一模型、独立初态和原题评分器，整轮冻结 Skill。报告逐阶段评分、目标错误复现情况和回归。原题链复测衡量已见题修复，独立留出评测衡量迁移。

归因修订后的候选 Skill 先在隔离诊断阶段比较修改前后表现及无关能力回归。正式 B/C 两组应统一采用同一冻结的优化器验证流程、诊断预算与反馈权限，不能只增强 C。

归因报告记录证据、定位状态及其与公开要求的关系。进化器根据已定位证据拟定候选规则。有确认的执行对照时，附上基于公开轨迹和重放的归因结论。控制器先校验引用，再用 Jev 的 Noul 判断是否采纳，同时用 Choice 在 `task` 和 `format` 中选择文档。采纳概率大于 0.5 时采用文档选择。`task` 存解题策略，`format` 存交付约束。未采纳的候选留在更新记录。每条候选单独校验；隐藏用例、参考答案和评分脚本留在隔离评估端。Jev 接收候选、引用的证据和当前 Skill。凭据在控制器通过 `TYPESAFE_API_KEY` 配置。 [TypeSafe System One API](https://docs.typesafe.ai/api/system-one)

进化器借鉴 ReasoningBank 的三段式记忆。`Title` 命名策略，`Description` 写适用条件或可观察的触发信号，`Content` 写判断、动作与验证方式。从成功轨迹提取有动作及结果支撑的决策，从失败轨迹提取与实际失分相关的错误机制和预防动作。训练时提供可用的评分项名称与状态，保留确切轨迹引用。修复建议应有因果证据，每条规则保留有迁移价值的领域信号，省去题号、实例数值及精确路径。评分结果与证据引用记录在更新回执中。 [ReasoningBank 提炼方法](https://arxiv.org/html/2509.25140v2#A1.SS1) · [作者提示词](https://github.com/google-research/reasoning-bank/blob/main/WebArena/prompts/memory_instruction.py)

从头验证时使用独立运行目录，从冻结 S0、原题和环境重新采集基线，并以本轮有效失败为扩题种子。新生成的候选经完整评分对照后进入 C。B/C 重新运行配对训练和 Skill 更新，评测重新运行并冻结各组的最终 Skill。记录同一轮内的配对结果及目标错误表现；跨轮比较同时受采样波动和题目变化影响。

诊断题、实现重放、前置验收、重试和离线补评单列成本，不计 B/C 正式训练次数或正式 Skill 更新。诊断样本不进入留出 Test。最终报告配对 C−B 表现、阶段与整链通过率、完整性、失败类型、token 和耗时；诊断正确不替代学习收益评测。

## 执行、恢复与记录

基线和扩题验收使用独立队列与锁，同时遵守主机资源限制。保留原始轨迹、提交归档、判定证据及重试尝试；缓存复用必须核对模型、Skill、任务、环境、提示、评分合同和预算身份。

探索性原题基线使用共享槽位池。调度优先派发已通过环境验收的独立题族，其次提前验收下一批题族，再派发证据齐备的 EVL 和离线对照。同一题族的四个环境对照可用独立工作目录并行执行。四个版本均通过 Oracle/NOP 后，Solver 按 EVO‑1→4 依次推进，每个版本须有正常完成且评分有效的轨迹。不同题族在各自的工作目录归档和评分，使用各自的文件锁。调度在每次派发前核对物理槽锁和磁盘余量。等待资源、等待前置和冷却状态单列记录，利用率按实际占用的物理槽锁统计。

真实 HTTP 429 等待至少 60 秒重试。直接请求重发相同输入；已终止的 Solver 在确认退出、归档及评分完成后，恢复相同逻辑位置。未知结果、认证错误、其他非重试错误、超步数和题目不合格不能冒充 429 重试；失败成本单列，有效训练次数不增加。

Solver 的验证应对应明确的未决问题，复用已有观察并在公开要求检查充分后收尾。收尾提示、循环检测或模型配置的调整需要独立验证，再统一应用于比较组；不能向 Solver 提供隐藏评分结果，或把程序评分通过自动改成轨迹完整。

## DeepSeek Harness 启动、实验调度与参考

| 脚本 | 职责 |
|---|---|
| [envharness_qwen_agent.py](pipeline/envharness_qwen_agent.py) | **启动 Solver**。创建 DeepSeek Harness 会话，调用 `harness.run(...)`，记录工具事件和轨迹 |
| [envharness_deepseek_designer_agent.py](pipeline/envharness_deepseek_designer_agent.py) | **启动出题 Agent**。由 `envharness_deepseek_expand.py` 调用，通过同一个 DeepSeek Harness SDK 执行交互式设计 |
| [envharness_expand.py](pipeline/envharness_expand.py) | 证据复核、模型接入与出题简报 |
| [envharness_deepseek_expand.py](pipeline/envharness_deepseek_expand.py) | 准备出题上下文和离线工作区，调用出题 Agent 并读取自检结果 |
| [looptool_terminal_adapter.py](pipeline/looptool_terminal_adapter.py) | 规则校验、语义审查及比较判断 |
| [envharness_adaptive.py](pipeline/envharness_adaptive.py) | 出题器和 Solver 共用的内部组件工具服务，执行组件、独立评分 |
| [envharness_abc_run.py](pipeline/envharness_abc_run.py) | B/C 配对练习、Skill 更新和 Test |
| [envharness_abc_preflight.py](pipeline/envharness_abc_preflight.py) | 完整链与身份检查 |

[EnvHarness 论文](https://arxiv.org/abs/2608.19880) · [EnvHarness 官方代码](https://github.com/google-research/envharness) · [LoopTool 论文](https://arxiv.org/html/2511.09148v1) · [LoopTool 官方 JGLV](https://github.com/DeepExperience/LoopTool/blob/main/dataloop/judgement_label_verification.py) · [复用源码指纹](pipeline/looptool_vendor/source.json)
