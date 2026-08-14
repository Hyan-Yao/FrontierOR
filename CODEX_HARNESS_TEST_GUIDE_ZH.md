# FrontierOR × GPT‑5.6 Sol × Codex harness 中文测试指南

## 1. 当前准备状态

截至 2026-08-02，本工作区已完成：

- 代码仓库：`FrontierOR`，commit `cd612c5`。
- 官方 Hugging Face 数据：`SmartOR/FrontierOR`，commit `37ccd8b6dca3bf7f4e0c58941a6ed156832a6d9e`。
- 数据已完整物化：180 个任务目录、5,583 个检出文件、Git LFS 指针数为 0；目录约 61 GB（包含工作树与 LFS 对象缓存）。
- Python：3.13.13；虚拟环境：`.venv`。
- 依赖：`gurobipy 12.0.3`、`numpy 2.2.6`、`scipy 1.16.1`、`requests 2.32.5`、`PyYAML 6.0.2`。
- Codex CLI：`0.144.5`；官方模型名为 `gpt-5.6-sol`。
- 已验证下面四个案例的 tiny/large JSON 均可解析，官方参考解可通过各自 feasibility checker。

进入环境：

```bash
cd /home/hyao/src/FrontierOR
source .venv/bin/activate
```

当前 Gurobi 能启动，但使用的是 2026-11-23 到期的 restricted non-production license。它足够做小模型自检，不保证能运行大型 Gurobi 模型。若要允许候选程序使用大规模 Gurobi，请配置完整 license：

```bash
export GRB_LICENSE_FILE=/绝对路径/gurobi.lic
```

## 2. 问题结构

FrontierOR 含 180 个论文驱动的运筹优化任务。每个 `frontier-or/<paper_id>/` 目录包含两类内容：

| 面向模型公开 | 评测时隐藏 |
|---|---|
| `problem_description.txt` | `mathematical_formulation.md` |
| `instance_schema.json` | `gurobi_code.py` |
| `solution_schema.json` | `instance/tiny_instance.json`、5 个 large instances |
| CLI、时限、日志要求 | `gurobi_solution/`、`gurobi_solution_log/` |
|  | `feasibility_check.py` |

模型的任务不是只写一个可执行 MIP，而是从自然语言识别隐含结构，输出能够在单 CPU、大实例、固定时限下得到高质量可行解的完整程序。官方论文的 Hard 子集有 50 个任务；进入 Hard 需要结构/规模困难，并由 Gurobi 一小时不收敛或仍有 gap 佐证。

本地 Codex harness 采用“生成与评分分离”：

```text
公开描述 + 两个 schema
        ↓
隔离的 /tmp workspace
        ↓  codex exec -m gpt-5.6-sol, xhigh
      code.py
        ↓
tiny gate（可行，且 gap ≤ 10%）
        ↓
5 个 large instances（每个独立运行）
        ↓
可执行性 → feasibility checker → gap@1% → QTE/AOCC
```

Codex 生成阶段由外层 Bubblewrap 隐藏整个 FrontierOR 仓库，只挂载 `/tmp` 中的公开 workspace，因此看不到真实实例、数学公式、checker、参考程序和参考解。评分阶段只给候选进程一个复制到临时目录的 instance，避免它沿路径读取答案。

## 3. 推荐的四个经典 hard cases

| case | 经典结构与规模 | 为什么适合测 Sol |
|---|---|---|
| `armbruster2012` | 最小图二分；平均约 13 万变量、45 万约束；五个参考运行一小时后 gap 仍约 97%–100% | 测图算法、松弛、局部改进，以及是否避免直接堆巨大整数模型 |
| `bront2009` | 选择模型下的航空 offer-set revenue management；168 个产品导致指数级列空间 | 测 column generation、定价子问题和最大化方向处理；也是论文案例研究 |
| `carvalho1999` | 一维 bin packing/cutting stock；每个 large instance 有 25,002 个物品 | 测构造启发式、模式/arc-flow、列生成与大规模输出；也是论文案例研究 |
| `schwerdfeger2016` | 14 台相同机器分配 280 个任务，最小化负载平方偏差 | 测 subset-sum、平衡启发式、局部搜索与小 gap 精度；也是论文案例研究 |

建议先跑前三个。它们覆盖图组合优化、指数列生成和大规模 packing；第四个用于补充“看似简单但目标非常敏感”的调度测试。

## 4. 手动启动 Codex 生成

每次用新的 run ID，避免后一次看到前一次的代码。以下命令不会调用 OpenRouter，而是使用你当前登录的 Codex：

```bash
bash scripts/codex_harness/run_case.sh sol-r1 armbruster2012
bash scripts/codex_harness/run_case.sh sol-r1 bront2009
bash scripts/codex_harness/run_case.sh sol-r1 carvalho1999
bash scripts/codex_harness/run_case.sh sol-r1 schwerdfeger2016
```

脚本固定以下条件：

- 模型：`gpt-5.6-sol`。
- reasoning effort：`xhigh`，适合 hard tasks。
- subagents：关闭，保持单 agent 协议。
- 非交互接口：`codex exec`。
- sandbox：`workspace-write`。
- 外层文件隔离：Bubblewrap 隐藏 FrontierOR 仓库，只暴露单个 case workspace。
- web search：关闭。
- 用户 MCP、skills、配置和 rules：不加载。
- rollout：`--ephemeral`；事件和 token usage 另存 JSONL。

产物位置：

```text
codex_harness/runs/sol-r1/
├── submissions/<paper_id>/gpt-5.6-sol/code.py
└── traces/<paper_id>/
    ├── events.jsonl
    └── final_message.md
```

不要从仓库根目录直接对 Codex 说“解决这个任务”。那会让 agent 看到 `gurobi_code.py`、真实实例与参考解，造成严重泄漏。

## 5. 先做低成本 pilot

先只评一个 large instance，每个实例给 300 秒：

```bash
.venv/bin/python scripts/codex_harness/evaluate_submission.py \
  --run-id sol-r1 \
  --paper-id armbruster2012 bront2009 carvalho1999 schwerdfeger2016 \
  --instances large_1 \
  --time-limit 300 \
  --exec-mode bubblewrap \
  --memory 8G \
  --memory-reserve 32G
```

评分脚本总是先跑 tiny gate；tiny 不过就不会烧 large instance 的算力。

当前机器上：

- `systemd-run --user` 无法连接 user bus。
- Snap Docker 因 capability 限制无法启动。
- 本指南增加的 `bubblewrap` backend 可用：固定单 CPU，把 candidate code/instance/output 映射到独立路径，并在候选进程中隐藏整个 FrontierOR checkout；它会在系统允许时额外创建禁网 namespace。当前受管会话不允许嵌套 network namespace，但父 sandbox 本身已经禁网。普通终端若不能创建该 namespace，必须用 Docker 才能宣称严格禁网。
- Bubblewrap 现在通过 `prlimit/RLIMIT_AS` 强制执行 `--memory`，不再忽略该参数；并用带文件锁的全局 reservation ledger 阻止多个 FrontierOR worker 同时吃掉保留给其他程序的 `--memory-reserve`。当前机器不能可靠创建 user-systemd cgroup，所以这是**单进程地址空间硬限制 + 并发准入**，不是 aggregate cgroup。需要严格限制整个进程树时仍应使用 Docker/cgroup。
- `bare` 仍可用于调试，但它只有 CPU pinning 和路径级隔离，不建议用于正式记录。

pilot 足以比较代码正确性和算法质量；若要和论文数字严格横向比较，应在 Docker 正常、Gurobi license 完整的机器上，用相同镜像、单 CPU 和禁网设置复跑。

## 6. 正式测试

正式设置使用五个 large instances、每例 3,600 秒：

```bash
.venv/bin/python scripts/codex_harness/evaluate_submission.py \
  --run-id sol-r1 \
  --paper-id armbruster2012 bront2009 carvalho1999 schwerdfeger2016 \
  --time-limit 3600 \
  --exec-mode bubblewrap \
  --memory 8G \
  --memory-reserve 32G
```

结果写入：

```text
codex_harness/runs/sol-r1/evaluation/
├── summary.md
├── summary.json
├── instances.csv
└── <paper_id>/...
```

核心指标：

- Execution：程序没有 runtime error。
- Feasibility：large solution 通过任务专属 checker。
- Solution quality：可行且相对 Gurobi 参考目标的方向感知 gap 不超过 1%。
- QTE：在达到 1% 质量门槛的同时，候选 wall time 不超过 Gurobi baseline。
- AOCC：对收敛日志的 gap-time 曲线积分，越低越好。

公开数据没有为所有任务统一提供 Gurobi wall time 字段。自定义评分脚本会优先从参考解读取时间，退而使用收敛日志；无法恢复时把 QTE 标为 `null/N/A`，不会编造数值。

## 7. 建议的实验设计

不要只跑一次。建议至少三个独立 run：

```text
sol-r1, sol-r2, sol-r3
```

每个 run 都重新生成四份 `code.py`，然后分别评分。最终同时报告：

1. case-level median，而不是只挑最好的一次。
2. tiny pass rate、large feasibility、quality@1%、QTE。
3. 生成阶段 token usage 与 wall time；不要把 agent 生成时间混进 solver runtime。
4. 失败分类：runtime、schema、infeasible、gap、timeout/license。
5. Codex CLI、代码/data commit、CPU、license、backend 和时限。

如果要对比论文里的 GPT‑5.3‑Codex one-shot，必须明确写成“Codex harness agentic variant”，不能直接声称是同协议模型升级。论文 one-shot 是单次生成；这里的 Codex harness 可以读写文件、执行静态/合成测试并自行迭代，能力边界更强。

## 8. 已发现的上游注意事项

1. 公开代码当前缺少 `results/data_statistics/paper_meta_info.csv` 和 `gurobi_results_*.csv`，所以 README 的原生 quick start 会在 direction registry 预检处失败。本指南的评分脚本直接读取公开 JSON metadata 和逐任务参考解，绕开这个缺口。
2. `one_shot_eval.py` 内部把候选 `pass` 的 gap 门槛设为 10%，而论文正式 Solution quality/QTE 门槛是 1%。本指南只用 10% 做 tiny gate，正式 quality 明确按 1% 计算。
3. 数据目录有 180 个任务，但 metadata JSON 当前只有 179 行，缺少 `ostrowski2012`。本指南所选四个任务都有 direction metadata。
4. `bierwirth2017` 很经典且 Gurobi 一小时仍有约 33%–46% gap，但当前 checker 对严格遵循公开 solution schema 的输出会触发 `processing_time` KeyError，因此暂不把它放入首轮正式集。
5. restricted Gurobi license 可能让生成程序的大模型求解直接失败；出现 “model too large for size-limited license” 应记为环境/license failure，再用完整 license 复跑，不能记成模型算法失败。

## 9. 依据

- [FrontierOR 论文](https://arxiv.org/html/2605.25246)：公开/隐藏组件、Hard 选择标准、单 CPU/Docker 约束、1% quality 与 QTE 定义。
- [FrontierOR 数据集](https://huggingface.co/datasets/SmartOR/FrontierOR)：任务文件结构与官方数据。
- [FrontierOR 任务页](https://frontieror.vercel.app/tasks.html)：逐任务表现和 self-evolution 案例标记。
- [Codex 官方手册：Models](https://learn.chatgpt.com/docs/models)：`gpt-5.6-sol`、reasoning effort。
- [Codex 官方手册：Non-interactive mode](https://learn.chatgpt.com/docs/non-interactive-mode)：`codex exec`、`--json`、sandbox 和 ephemeral 运行。
