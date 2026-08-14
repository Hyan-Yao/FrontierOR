# FrontierOR 多 Agent 框架对比：GPT-5.6 Sol + Codex Harness

评测日期：2026-08-02  
模型：`gpt-5.6-sol`  
case：`armbruster2012`、`bront2009`、`carvalho1999`、`schwerdfeger2016`

## 1. 结论先行

- 按“`large_1` 可行且 gap ≤ 10%”计算，Codex seed、OpenEvolve、EoH、CORAL 都是 **3 成功 / 1 失败**；失败 case 都是 `carvalho1999`。
- 在严格 closed-book 条件下，**EoH 是本次最有价值的增量框架**：
  - `armbruster2012` 的 gap 从同口径 Codex baseline 的 3.16% 降到 2.81%；
  - `carvalho1999` 从 11.50% 降到 10.48%，距离成功门槛只差 0.48 个百分点。
- **OpenEvolve 没有带来净增益**：成功数不变，`armbruster2012` 反而从 3.16% 退化到 5.26%。单一 dev AOCC 选择器在该 case 上奖励了泛化更差的程序。
- **CORAL 在 `armbruster2012` 上产生了非常强的探索结果**：可行解目标 41804，优于参考 51158 约 18.28%。但本轮 CORAL 实际启用了网页研究，因此只能视为 **web-assisted exploratory**，不能与 closed-book Codex/OpenEvolve/EoH 直接排名。
- `bront2009` 和 `schwerdfeger2016` 的 Codex seed 已经接近或达到最优；额外框架主要增加成本，没有稳定改善。

## 2. 公平性与限制

本报告把结果分成两组：

1. **严格可比（closed-book）**：Codex seed、OpenEvolve、EoH。候选 solver 不能联网，也看不到 API key、Codex 登录、参考解目录或隐藏 grader。
2. **探索性（web-assisted）**：CORAL。虽然 task YAML 设置了 `research: false`，旧 Codex wrapper 没有把它转换成现代 CLI 的 `web_search=false`；日志确认 agent 搜索了公开论文和算法资料。它没有读取本地标准解或隐藏 grader，最终解也通过 verifier，但不满足严格 closed-book 条件。

该问题已在 harness 中修复：新 CORAL task 会显式传入 `runtime_options.web_search=false`。因此下面的 CORAL 数字可以用来判断框架潜力，但不应写入严格 leaderboard。

其他限制：

- 这是低预算 pilot，不是完整统计实验；每个框架只跑了一次随机轨迹。
- EoH 只使用 `population=1`、`1 generation`、`m1`，即每个 case 一个真实变体。
- OpenEvolve 使用 3 iterations；CORAL 使用 baseline + 1 个真实改码 attempt。
- `armbruster2012` 的启发式包含一定运行波动，因此小幅目标/时间差异需要多 seed 复验。
- 参考目标不一定是数学最优值。例如 CORAL 在 `armbruster2012` 找到了比参考值更好的可行解；评测器把这种情况的 gap 截断为 0%。

## 3. 统一测试协议

| 项目 | 配置 |
|---|---|
| 初始程序 | 四个框架均复用同一份 GPT-5.6 Sol Codex harness `code_attempt0.py` |
| tiny gate | `tiny`，300 秒，可行且 gap ≤ 10% |
| 进化集 | `large_2`，300 秒，AOCC scorer |
| 留出测试 | `large_1`，300 秒；不参与进化或选择 |
| 执行隔离 | Bubblewrap，单 CPU；候选看不到 key/Codex auth |
| 成功标准 | `large_1` 可行且 gap ≤ 10% |
| 随机种子 | 框架 seed 42；solver 内部未必完全确定 |
| Gurobi | 本地 restricted license，候选强制 `Threads=1` |

选择 `large_2` 做 dev、`large_1` 做 held-out，是为了避免“在最终测试实例上进化”。AOCC 同时考虑收敛轨迹、质量和时间；本报告仍把 held-out gap 作为最终判断，防止 dev score 过拟合。

## 4. 框架预算

| 方法 | 本轮预算 | 主要机制 |
|---|---:|---|
| Codex baseline | 1 个 seed | GPT-5.6 Sol 一次生成完整 solver |
| OpenEvolve | seed + 3 iterations | MAP-Elites、LLM 全程序变异、按 dev AOCC 选择 |
| EoH | seed + 1 个 `m1` 变体 | 单亲变异；本轮为最小有效配置 |
| CORAL | baseline + 1 个变体 | Codex coding agent、notes/skills、测试、commit、隐藏 grader feedback |

版本：OpenEvolve `v0.2.27`；EoH commit `bc1d881`；CORAL commit `61bc761`（`0.5.1.dev1+g61bc7619a`）。

## 5. `large_1` 留出集总表

`gap` 对最小化问题按 `(candidate-reference)/reference` 计算；负数表示优于参考。`≈0%` 表示浮点误差量级的近精确结果。

| case | 方法 | 目标值 | gap | 时间 | 结果 |
|---|---|---:|---:|---:|---|
| armbruster2012 | Codex baseline（同口径重测） | 52776 | 3.16% | 3.12s | 成功 |
|  | OpenEvolve | 53847 | 5.26% | 4.24s | 成功，但退化 |
|  | EoH | 52598 | 2.81% | 5.16s | 成功，closed-book 最佳 |
|  | CORAL（web-assisted） | **41804** | **-18.28%** | 5.93s | 成功，探索性最佳 |
| bront2009 | Codex baseline（同口径重测） | 243985.563175 | ≈0% | 0.35s | 成功 |
|  | OpenEvolve | 243985.563175 | ≈0% | 0.74s | 成功 |
|  | EoH | 243985.563175 | ≈0% | 1.36s | 成功 |
|  | CORAL（web-assisted） | 243985.563175 | ≈0% | 0.61s | 成功 |
| carvalho1999 | Codex baseline（同口径重测） | 9292 | 11.50% | 0.24s | **失败** |
|  | OpenEvolve | 9292 | 11.50% | 0.23s | **失败** |
|  | EoH | **9207** | **10.48%** | 0.38s | **失败，接近门槛** |
|  | CORAL（web-assisted） | 9292 | 11.50% | 1.22s | **失败** |
| schwerdfeger2016 | Codex baseline（同口径重测） | 0.000537341198 | 0% | 0.03s | 成功 |
|  | OpenEvolve | 0.000537341198 | 0% | 0.03s | 成功 |
|  | EoH | 0.000537341198 | 0% | 0.03s | 成功 |
|  | CORAL（web-assisted） | 0.000537341198 | 0% | 0.03s | 成功 |

### 成功数

| 方法 | 成功 | 失败 |
|---|---:|---:|
| Codex baseline | 3 | 1 |
| OpenEvolve | 3 | 1 |
| EoH | 3 | 1 |
| CORAL（web-assisted，不纳入严格排名） | 3 | 1 |

框架没有增加成功 case 数，但 EoH 改善了两个困难 case 的质量，CORAL 则在 arm 上显示出较高的 agentic search 上限。

## 6. Dev 集结果与泛化

框架内部 score 为 `1 - AOCC`，越高越好。

| case | OpenEvolve | EoH | CORAL（web-assisted） |
|---|---:|---:|---:|
| armbruster2012 | 0.517952 | 0.532274 | **0.748768** |
| bront2009 | 0.837068 | **0.916953** | 0.765568 |
| carvalho1999 | 缺失（best 未过 gate） | **0.980503** | 0.973026 |
| schwerdfeger2016 | 1.000000 | 1.000000 | 1.000000 |

注意：不同框架的 selected trajectory 和日志密度不同，AOCC 不应脱离最终 gap 单独解释。

- OpenEvolve 在 arm 上选择了 dev score 略高但 held-out 更差的程序，是明显的 selection overfit。
- EoH 在 carvalho dev 上达到目标 10021、参考 10010，约 0.11% gap；held-out 仍为 10.48%。
- CORAL 在 carvalho dev 上达到目标 10026、参考 10010，约 0.16% gap；held-out 回到 11.50%。这是最典型的 dev-to-test 泛化失败。

## 7. 逐 case 解释

### 7.1 armbruster2012：带容量约束的最小图二分

- **任务**：把带节点权重的无向图分成两组，每组重量不超过容量，最小化跨组边成本。
- **参考**：`large_1` 参考目标 51158。
- **Codex seed**：紧凑 MILP + 贪心 warm start + 局部改进；同口径重测目标 52776，gap 3.16%。
- **OpenEvolve**：内部 AOCC 选择偏向了更差的候选，held-out 目标 53847、gap 5.26%。
- **EoH**：重写为多起点局部搜索 + 单线程 Gurobi；目标 52598、gap 2.81%，质量小幅提升但速度变慢。
- **CORAL**：加入容量可行的 variable-depth Kernighan–Lin swap refinement；目标 41804，两个分区各 8500 个节点，verifier 报告零违规。这是强结果，但使用了公开论文/算法网页研究，必须 closed-book 重跑确认。
- **改进建议**：dev 不要只用一个实例；选择器先按 signed gap/可行性排序，再用 AOCC 打破平局。对 CORAL 严格关闭 web 后至少跑 3 个 seed。

### 7.2 bront2009：混合 MNL 选择下的网络收益管理 CDLP

- **任务**：为不同非空 offer set 分配展示时间，满足航段容量和总时间约束，最大化期望收入。
- **参考**：`large_1` 参考约 243985.563419；所有方法都得到 243985.563175，差异仅为浮点量级。
- **Codex seed**：列生成 + 精确定价/启发式定价已经足够强。
- **框架结果**：三种框架都保持近精确解，但没有在 held-out 上稳定加速；EoH 最慢。
- **改进建议**：该 case 不应继续投入大量 mutation 预算。加入“seed 已在 dev 达到质量上限则 early stop”的规则，节省模型调用。

### 7.3 carvalho1999：一维 bin packing

- **任务**：把所有 item 放入容量相同的 bins，最小化使用的 bin 数。
- **tiny gate**：参考 20；Codex seed 为 24（20% gap），所以原 harness 在 tiny 就失败。
- **参考**：`large_1` 参考 8334；成功门槛对应候选目标不高于约 9167。
- **OpenEvolve**：最好变体只把 tiny 从 24 改到 23（15% gap），仍未过 gate；最终 held-out 9292。
- **EoH**：tiny 修到 20；dev `large_2` 为 10021（参考 10010）；held-out 为 9207、10.48%，已经非常接近成功。
- **CORAL**：tiny/dev 明显改善，dev score 0.973026；但 held-out 仍为 9292、11.50%，属于 dev overfit。
- **改进建议**：把两个不同尺寸/分布的实例放入 dev；EoH 再增加 2–4 次 `m1/m3` 变异，重点对 9207 解做局部 bin elimination，很可能跨过 10% 门槛。

### 7.4 schwerdfeger2016：相同并行机负载均衡

- **任务**：把 jobs 分配到相同机器，最小化归一化平方负载偏差。
- **参考**：`large_1` 目标约 0.000537341198。
- **结果**：Codex seed 已精确；所有框架都保持精确解和约 0.03 秒运行时间。
- **框架行为**：EoH/CORAL 生成了 exact subset / pair repartition 等更复杂代码，但 held-out 无可测增益。
- **改进建议**：把它作为 regression/no-harm case，而不是主要进化目标；达到精确后立即 early stop。

## 8. 本轮发现并修复的 harness 问题

1. 支持直接使用 `OPENAI_API_KEY`，不再强制 OpenRouter。
2. GPT-5/o-series 调用去掉不支持的 temperature，并使用 `max_completion_tokens`。
3. OpenEvolve preflight 支持官方 OpenAI model endpoint。
4. 元数据 CSV 缺失时，从 `paper_meta_info.json` 回退读取优化方向。
5. 新增 Bubblewrap backend；候选看不到 API key/Codex auth，只挂载所需解释器、venv、实例和输出目录。
6. EoH paper-level 并行会因全局 monkey-patch/env 串扰而污染结果；入口现在自动强制 `--paper-workers=1`。
7. CORAL 不再使用 `--dangerously-bypass-approvals-and-sandbox`，改为 `workspace-write`。
8. CORAL worktree PATH 会回退到 FrontierOR `.venv`，确保 `coral eval` 可调用。
9. CORAL resume 改为从 agent worktree 读取 `.coral_dir` breadcrumb，不再假设默认 `results/` 目录。
10. CORAL 新任务强制 `web_search=false`。
11. Codex sandbox 保护 `.git` 时，controller 只在日志确认 agent 主动 eval 且遇到只读 `index.lock` 后代为提交候选。

## 9. 下一轮推荐配置

为了得到可发表/可汇报的结论，建议下一轮：

- 每种方法、每个 case 至少 3 个随机 seed；报告均值、中位数、最好值和成功率。
- 统一“真实修订候选数”，例如每个 case 10 个，而不是按框架默认 iteration/generation 名称对齐。
- dev 至少使用两个实例；`large_1` 永远只做最终 held-out。
- 选择顺序：可行性 → signed gap → AOCC → 时间。不要只按 AOCC。
- CORAL 使用修复后的 closed-book 配置重新跑；其本轮 web-assisted arm 结果只能作为待验证假设。
- 优先增加 `carvalho1999` 的 EoH 预算；bront/schwer 达到上限后 early stop。

建议的中等预算：

```bash
cd /home/hyao/src/FrontierOR

# OpenEvolve：10 个真实变体
.venv/bin/python -u test_time_self_evolution/run_eval_modes.py \
  --framework openevolve --openevolve-iterations 10 \
  --primary-model gpt-5.6-sol \
  --paper-id armbruster2012 bront2009 carvalho1999 schwerdfeger2016 \
  --paper-workers 2 --dev-set large_2 --test-set large_1 \
  --stage1-time-limit 300 --stage2-time-limit 300 --test-time-limit 300 \
  --stage2-scorer aocc --stage2-time-policy uniform --test-time-policy uniform \
  --exec-mode bubblewrap --cpus 1 --memory 16G \
  --run-id gpt56-oe-medium

# EoH：串行 paper；2 个个体 × 2 代 × m1/m3
.venv/bin/python -u test_time_self_evolution/run_eval_modes.py \
  --framework eoh --eoh-pop-size 2 --eoh-n-pop 2 \
  --eoh-workers 2 --eoh-operators m1 m3 --eoh-timeout 660 \
  --primary-model gpt-5.6-sol \
  --paper-id armbruster2012 bront2009 carvalho1999 schwerdfeger2016 \
  --paper-workers 1 --dev-set large_2 --test-set large_1 \
  --stage1-time-limit 300 --stage2-time-limit 300 --test-time-limit 300 \
  --stage2-scorer aocc --stage2-time-policy uniform --test-time-policy uniform \
  --exec-mode bubblewrap --cpus 1 --memory 16G \
  --run-id gpt56-eoh-medium

# CORAL：修复后为 closed-book；baseline + 3 个 agent attempts
.venv/bin/python -u test_time_self_evolution/run_eval_modes.py \
  --framework coral --coral-attempts 4 --coral-max-seconds 2400 \
  --coral-agent-runtime codex --coral-agent-count 1 \
  --coral-agent-model gpt-5.6-sol --coral-max-turns 20 \
  --primary-model gpt-5.6-sol \
  --paper-id armbruster2012 bront2009 carvalho1999 schwerdfeger2016 \
  --paper-workers 2 --dev-set large_2 --test-set large_1 \
  --stage1-time-limit 300 --stage2-time-limit 300 --test-time-limit 300 \
  --stage2-scorer aocc --stage2-time-policy uniform --test-time-policy uniform \
  --exec-mode bubblewrap --cpus 1 --memory 16G \
  --run-id gpt56-coral-closed-medium
```

## 10. 结果与代码位置

- 原 Codex 评测：`codex_harness/runs/sol-r1/evaluation/summary.md`
- OpenEvolve test CSV：`eval/eval_test_results_openevolve.csv`
- EoH test CSV：`eval/eval_test_results_eoh.csv`，有效 run-id 为 `gpt56-framework-pilot-serial`
- CORAL test CSV：`eval/eval_test_results_coral.csv`，有效 run-id 为 `gpt56-framework-pilot-coral-safe`
- OpenEvolve artifacts：`eval/openevolve/gpt56-framework-pilot/`
- EoH artifacts：`eval/eoh/gpt56-framework-pilot-serial/`
- CORAL artifacts：`eval/coral/gpt56-framework-pilot-coral-safe/`
- 先前 case-study HTML：`FRONTIEROR_GPT56_SOL_CASE_STUDY_ZH.html`

## 11. 最终判断

如果问题是“其他 agent 框架能否把 GPT-5.6 Sol 的 4 个 hard case 成功数从 3/4 提高到 4/4”，本轮答案是：**不能**。

如果问题是“框架是否能改善 solver 质量”，答案是：

- **EoH：能，且是本轮严格可比条件下最可信的增益。**
- **OpenEvolve：本轮不能，选择器还出现了过拟合。**
- **CORAL：潜力最大，但本轮使用了网页研究，必须 closed-book 重跑后才能下正式结论。**
