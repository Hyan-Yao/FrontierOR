# FrontierOR 内存安全 Sandbox

## 结论

当前工作站无法可靠使用 `systemd-run --user` transient cgroup，因此 FrontierOR 的 `bubblewrap` backend 采用：

1. Bubblewrap 文件系统与环境隔离；
2. `prlimit/RLIMIT_AS` 限制每个候选 Python/Gurobi 进程的虚拟地址空间；
3. `/tmp/frontieror_memory_admission.*` 全局 reservation ledger，跨 paper/process worker 做启动准入；
4. `--memory-reserve` 为其他程序保留 host `MemAvailable`；
5. `OMP_NUM_THREADS`、`OPENBLAS_NUM_THREADS`、`MKL_NUM_THREADS`、`NUMEXPR_NUM_THREADS` 固定为 1。

`--memory` 现在是实际限制，不再被 Bubblewrap 忽略。若 `prlimit` 不存在，backend 会 fail closed，拒绝启动无内存上限的候选。

## 与其他程序共用机器时的推荐设置

保守配置：每个候选最多 8 GiB，至少给其他程序留下 32 GiB，并把所有显式 worker 设为 1：

```bash
cd /home/hyao/src/FrontierOR

.venv/bin/python -u test_time_self_evolution/run_eval_modes.py \
  --framework openevolve \
  --primary-model gpt-5.3-codex \
  --paper-id armbruster2012 \
  --paper-workers 1 \
  --dev-instance-workers 1 \
  --test-instance-workers 1 \
  --exec-mode bubblewrap \
  --cpus 1 \
  --memory 8G \
  --memory-reserve 32G \
  --run-id memory-safe-example
```

`configs/openevolve.yaml` 的 `parallel_evaluations` 已降为 1，避免 OpenEvolve 在一个 paper 内同时启动两个大模型候选。

Codex submission 评分：

```bash
.venv/bin/python scripts/codex_harness/evaluate_submission.py \
  --run-id <run-id> \
  --model <model-id> \
  --paper-id <paper-id> \
  --instances large_1 \
  --time-limit 300 \
  --exec-mode bubblewrap \
  --cpus 1 \
  --memory 8G \
  --memory-reserve 32G
```

## 两个参数如何工作

- `--memory 8G`：每个候选进程的 `RLIMIT_AS`。Python、NumPy 和 Gurobi 在超过地址空间上限时会收到分配失败，而不是继续把主机推向 OOM。
- `--memory-reserve 32G`：启动新候选前，准入器检查当前 `MemAvailable`、其他 FrontierOR active reservations、新候选上限和 reserve；不满足就直接返回 `memory admission denied`。

并发 reservation 是保守计算：已启动候选按完整上限计入，即使它尚未真正用满内存。这样会降低吞吐，但适合与重要工作负载共用机器。

## 限制

`RLIMIT_AS` 是**每进程**限制，不是整个进程树的 aggregate memory cgroup。限制会被子进程继承，但多个子进程理论上可各自使用到同一上限。FrontierOR 协议要求 solver 单进程、单 CPU，因而当前实现能覆盖正常候选；对恶意代码或允许多进程的 workload，应使用 Docker：

```bash
--exec-mode docker --memory 8G
```

Docker 的 `--memory`/cgroup 才能对容器内所有进程实施 aggregate hard cap。当前受管会话不能可靠连接 user systemd bus，因此没有把 `systemd-run` 当作可用保证。

## 验证

轻量测试不会运行真实 FrontierOR case：

```bash
.venv/bin/python -m unittest discover \
  -s tests -p 'test_exec_backends_memory.py' -v
```

测试覆盖：

- memory size 解析；
- host reserve 不足时拒绝启动；
- 256 MiB 上限内的小程序正常完成；
- 128 MiB 上限下的 256 MiB 分配被阻止；
- reservation ledger 在退出后清理为空。
