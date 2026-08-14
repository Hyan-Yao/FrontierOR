# FrontierOR GPT-5.3 四 Harness 实验总结（部分完成）

评测日期：2026-08-02 至 2026-08-03  
实际模型：`gpt-5.3-codex`  
case：`armbruster2012`、`bront2009`、`carvalho1999`、`schwerdfeger2016`

## 1. 结论先行

- **Codex baseline 完整跑完，结果为 0 成功 / 4 失败。**
- **OpenEvolve 可判定为 0 成功 / 4 失败。** 三个 case 有完整 `large_1` 失败记录；`bront2009` 没有完成 held-out，但 seed 和全部变体都已明确未过 mandatory tiny gate，因此仍可判为失败。
- **EoH 只完成 2/4 个 case，完成的两个都是失败。** `carvalho1999` 在 seed dev 期间被停止，`schwerdfeger2016` 未开始，不能把 EoH 写成 0/4。
- **CORAL 的 GPT-5.3 实验没有开始。**
- 因用户观察到 OOM，所有剩余实验已停止。因此，本轮只能总结部分轨迹，**不能形成四 harness 的完整公平排名**。
- 当前最明显的失败模式不是小幅 gap，而是 solver 结构不可扩展：超大直接 MIP、指数级 offer-set 枚举，以及 300 秒超时。

## 2. 实验完整度

| Harness | armbruster2012 | bront2009 | carvalho1999 | schwerdfeger2016 | 可报告结论 |
|---|---|---|---|---|---|
| Codex baseline | 完成，失败 | 完成，tiny 失败 | 完成，失败 | 完成，失败 | 0/4 |
| OpenEvolve | 最终评测完成，失败；计划 3 次变异中完成 2 次 | 3 次变异完成；tiny 失败，held-out 未完成 | 完成，失败 | 完成，失败 | gate-aware 口径下 0/4 |
| EoH | 完成，失败 | 完成，tiny 失败 | 中断，无最终结果 | 未运行 | 已完成部分 0/2 |
| CORAL | 未运行 | 未运行 | 未运行 | 未运行 | 无数据 |

这里的“成功”沿用统一标准：mandatory tiny gate 可行且 gap 不超过 10%，最终 `large_1` 可行且 gap 不超过 10%。

## 3. 统一协议

| 项目 | 配置 |
|---|---|
| 模型 | `gpt-5.3-codex` |
| Codex reasoning effort | `xhigh` |
| tiny gate | `tiny`，300 秒，可行且 gap ≤ 10% |
| dev | `large_2`，300 秒，AOCC，uniform time policy |
| held-out | `large_1`，300 秒，uniform time policy |
| 候选执行 | Bubblewrap、单 CPU、无网络 |
| OpenEvolve | seed + 3 iterations；arm 实际只完成 2 个变体 |
| EoH | `pop_size=1`、`n_pop=1`、`m1`，每 case 一个真实变体 |
| CORAL | 原计划 baseline + 1 个变体，但未运行 |

注意：评测时没有配置 `GRB_LICENSE_FILE`，实际使用的是本机 Gurobi restricted/size-limited license。评测当时的 Bubblewrap backend 固定单核，但忽略了 `--memory 16G`；这会放大超大 MIP 的内存风险，也是本轮 OOM 后停止实验的重要环境限制。该问题随后已修复：Bubblewrap 现在使用 `RLIMIT_AS` 和全局 memory admission ledger；严格的进程树 aggregate cap 仍需 Docker/cgroup。

## 4. Codex baseline：完整结果

run ID：`gpt53-codex-api-r1`

| case | tiny gate | `large_1` 结果 | 时间 | 主要失败原因 |
|---|---|---|---:|---|
| armbruster2012 | 通过：目标 58，gap 0% | 失败 | 1.61s | 完整图二分 MIP 超过 size-limited Gurobi 模型上限 |
| bront2009 | **失败**：4860 vs 7900，gap 38.48% | 跳过 | — | tiny 已不合格；程序枚举所有非空 offer sets |
| carvalho1999 | 通过：目标 20，gap 0% | 失败 | 334.48s | FFD 后构造完整 bin-packing MIP，外部 300 秒超时 |
| schwerdfeger2016 | 通过：目标约 0.316336，gap 0% | 失败 | 0.04s | 完整 assignment MIQP 超过 size-limited Gurobi 模型上限 |

成功数：**0/4**。

### 4.1 生成程序的结构

- `armbruster2012`：每个节点一个二进制变量、每条边一个 cut 变量的完整 MIP；带 greedy fallback，但 Gurobi 异常没有被捕获，因此 fallback 没有执行。
- `bront2009`：使用 `itertools.combinations` 枚举所有非空产品集合，再建立 LP。该算法在 tiny 上目标质量已经不足，在大实例上还存在指数复杂度。
- `carvalho1999`：先 FFD，再建立 `x[i,b]` 与 `y[b]` 的完整 MIP。tiny 精确，但大实例无法在时限内完成。
- `schwerdfeger2016`：建立 machine-job 全 assignment MIQP；tiny 有效，大实例触发 license 尺寸限制。

### 4.2 Codex 调用记录

四个 baseline 合计：

- input tokens：164,359
- cached input tokens：118,784
- output tokens：8,184
- trace 中记录的 reasoning output tokens：0

Codex CLI 在 API-key 模式下成功调用模型，但提示本地缺少该模型的 metadata，使用 fallback metadata。这个警告没有阻止代码生成，但属于解释结果时应保留的兼容性 caveat。

## 5. OpenEvolve：低预算结果

有效 run ID：`gpt53-framework-pilot-oe-responses`

| case | 变异情况 | 选择结果 | `large_1` | 判断 |
|---|---|---|---|---|
| armbruster2012 | 计划 3 次，完成 2 次后会话中断 | seed，dev score 0 | runtime error，1.47s | 失败 |
| bront2009 | 3 次完成；全部未修过 tiny | seed | held-out 未完成 | tiny gate 已判失败 |
| carvalho1999 | 3 次完成；候选 dev 均 runtime error | seed | runtime error，332.52s | 失败 |
| schwerdfeger2016 | 3 次完成；候选均未避开超大 MIQP | seed | runtime error，0.04s | 失败 |

### 5.1 关键观察

- 三个有完整 held-out 的 case，OpenEvolve 都选择了 generation 0 的原 seed。
- `carvalho1999` 三个变体均通过 tiny，但 `large_2` 全部 runtime error；其中一个变体 tiny 目标为 21、gap 5%，并在 tiny 与 dev 分别消耗约 300 秒和 332 秒。
- `bront2009` 的变体 tiny gap 最好仍未达到 10% 门槛，因此没有得到可扩展列生成方案。
- 所有已选择程序的 dev combined score 都是 0，说明低预算进化没有建立有效的正向选择信号。

gate-aware 结果：**0/4**。不过 arm 少完成一次计划变异、bront 缺 held-out，所以它不是严格意义上的完整 4×3 iteration run。

## 6. EoH：仅完成两个 case

有效 run ID：`gpt53-framework-pilot-eoh-responses`

| case | seed | `m1` 变体 | 最终结果 |
|---|---|---|---|
| armbruster2012 | tiny 精确；dev 因超大 MIP失败 | 改为随机构造 + 1-flip local search + MIP refine；tiny 精确但耗时 123.29s，dev runtime error 108.17s | 选择 seed；`large_1` runtime error 1.49s，失败 |
| bront2009 | tiny 4860，gap 38.48% | 改为 restricted-column LP，但 tiny 仍为 4860、gap 38.48% | mandatory tiny gate 失败；held-out 跳过 |
| carvalho1999 | seed tiny 目标 20、通过；dev 运行期间停止 | `m1` 尚未生成 | 无最终结果 |
| schwerdfeger2016 | 未运行 | 未运行 | 无结果 |

EoH 在已完成部分为 **0 成功 / 2 失败**。不能推断为 0/4。

值得注意的是，EoH 确实产生了比 OpenEvolve 更有结构性的重写：arm 加入了构造式启发式和局部搜索，bront 尝试 restricted-column LP。但在一个真实变体的预算下，两者都没有跨过最终门槛。

## 7. CORAL

GPT-5.3 CORAL 实验没有启动，因此没有结果，不能与其他三个 harness 比较。

## 8. API 与 harness 兼容性发现

本轮开始时遇到两类不应计入模型失败的接口问题：

1. `gpt-5.3-codex` 在 OpenAI API 项目中可查询，但当前 Codex CLI 的 ChatGPT-account 通道拒绝该模型；baseline 最终改为临时隔离的 API-key Codex home。
2. 该模型不支持 `/v1/chat/completions`，要求 `/v1/responses`。OpenEvolve 与 EoH 已增加 Responses API 适配；适配前产生 400 的无效 run 已排除。

有效结果只使用带 `-responses` 的 OpenEvolve/EoH run ID。密钥从项目 `.env` 加载，文件权限已收紧为 `600`，报告和日志没有输出密钥值。

## 9. OOM 与停止点

用户观察到实验导致 OOM 后，剩余实验被主动停止。停止时：

- OpenEvolve：除 bront held-out 外基本完成；bront 已能由 tiny gate 判失败。
- EoH：arm、bront 完成；carvalho 中断；schwer 未运行。
- CORAL：四个 case 均未运行。

停止后已确认没有残留 `run_eval_modes`、OpenEvolve、EoH 或 CORAL 评测进程。停止检查时系统约有 117 GiB available memory，但这只说明终止后已恢复，不能用于反推峰值占用。

## 10. 与 GPT-5.6 pilot 的有限对照

先前 GPT-5.6 Sol pilot 中，Codex baseline、OpenEvolve、EoH 都是 3/4；CORAL 的 web-assisted 探索结果也是 3/4。GPT-5.3 本轮 baseline 为 0/4，差距很大。

但不能把这个差异直接解释为纯模型能力差距，原因包括：

- GPT-5.3 通过 API-key Codex/Responses 路径运行，GPT-5.6 Sol 使用另一模型与服务路径。
- GPT-5.3 Codex CLI 使用 fallback model metadata。
- 本机是 size-limited Gurobi 环境；GPT-5.3 恰好更频繁生成完整大 MIP。
- GPT-5.3 的 EoH/CORAL 实验不完整，且因 OOM 停止。

可以可靠表达的结论是：**在本次具体 harness、license 和低预算设置下，GPT-5.3 生成的 solver 明显缺乏大实例可扩展性；现有 OpenEvolve/EoH 预算不足以稳定修复这一问题。**

## 11. 结果位置

- Codex baseline summary：`codex_harness/runs/gpt53-codex-api-r1/evaluation/summary.json`
- Codex traces：`codex_harness/runs/gpt53-codex-api-r1/traces/`
- GPT-5.3 seeds：`eval/eval_papers/<case>/gpt-5.3-codex/code_attempt0.py`
- OpenEvolve artifacts：`eval/openevolve/gpt53-framework-pilot-oe-responses/`
- OpenEvolve test CSV：`eval/eval_test_results_openevolve.csv`
- EoH artifacts：`eval/eoh/gpt53-framework-pilot-eoh-responses/`
- EoH test CSV：`eval/eval_test_results_eoh.csv`
- GPT-5.6 对照报告：`FRONTIEROR_AGENT_FRAMEWORK_COMPARISON_ZH.md`

## 12. 最终判断

若问题是“GPT-5.3 在四个 harness、四个 case 上谁最好”，本轮答案是：**数据不足，不能排名**。

若问题是“已经跑出的 GPT-5.3 solver 表现如何”，答案是：

- Codex baseline：完整结果 0/4。
- OpenEvolve：没有恢复任何成功 case，gate-aware 结果 0/4。
- EoH：完成的两个 case 都没有恢复成功；另外两个缺失。
- CORAL：没有数据。

本轮最重要的工程结论是：后续若恢复实验，应使用新加入的 `RLIMIT_AS`/memory admission 防护；若需要严格限制整个进程树，则改用 Docker/cgroup。同时应在 prompt/evaluator 中强制避免超大完整 MIP 和指数级全枚举，否则增加 agent iteration 很可能只是重复触发超时或 OOM。
