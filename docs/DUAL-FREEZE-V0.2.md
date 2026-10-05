# DualView Static Action-Pair v0.2

## 版本定位

这是 Gen 9 Random Battle 的第一版可实战观察冻结候选，不是完整的 belief-aware MCTS。

本版本实现：

1. 我方真实私有视角和对手公开视角分离；
2. 对手隐藏配置的 possible-world sampling；
3. 我方和对手独立调用 Foul Play policy oracle；
4. 独立的我方和对手搜索预算；
5. 按每个 world 内归一化后的 policy 聚合对手动作分布；
6. 对手策略和均匀探索的显式混合；
7. 基于 poke-engine damage rolls 的静态 action-pair value；
8. 根节点动作重加权；
9. 每个 particle 的搜索诊断和 action-pair 诊断；
10. PS live shadow comparison 和 JSONL 记录。

## 概率边界

本版本明确区分三种概率：

- `particle weight`：隐藏配置 possible world 的 belief 权重；
- `selfPolicy` 和 `opponentModelPrediction`：Foul Play policy oracle 的动作分布；
- `opponentPrediction`：加入均匀探索后的对手策略分布；
- 命中、伤害和暴击等游戏随机性：继续由 poke-engine 处理，不由对手策略混合改写。

`8 x 7` 只表示双方合法动作的联合空间，不表示 56 个联合动作等概率。

## 推荐冻结配置

先固定一组配置，不要在同一局中同时调整多个变量：

```text
format: gen9randombattle
particles: 1
self-search-time-ms: 5
opponent-search-time-ms: 5
opponent-exploration: 0.1
seed: 17
strength: 1.0
```

如果延迟稳定，再单独测试：

```text
self-search-time-ms: 10
opponent-search-time-ms: 10
```

## 启动命令

8093 reference：

```powershell
& D:\Other\ai\foul-play-lab\.venv\Scripts\python.exe `
  D:\Other\ai\foul-play-lab\tools\live_reference_server.py `
  --host 127.0.0.1 `
  --port 8093 `
  --particles 1 `
  --search-time-ms 50 `
  --seed 17
```

8094 DualView：

```powershell
& D:\Other\ai\foul-play-lab\.venv\Scripts\python.exe `
  D:\Other\ai\foul-play-lab\tools\dual_view_server.py `
  --host 127.0.0.1 `
  --port 8094 `
  --particles 1 `
  --self-search-time-ms 5 `
  --opponent-search-time-ms 5 `
  --opponent-exploration 0.1 `
  --seed 17 `
  --strength 1.0
```

## 冻结前验收

必须全部满足：

- `8093/health` 返回成功；
- `8094/health` 返回成功；
- `8094/choice` 返回合法 PS action；
- 返回 `selfSearchTimeMs` 和 `opponentSearchTimeMs`；
- 返回 `opponentModelPrediction` 和 `opponentPrediction`；
- 返回 `particleDiagnostics`；
- 返回 `actionPairValues`；
- 动作概率和为 1；
- 没有异常、非法动作或服务端 fallback；
- live observer JSONL 能保存 DualView 诊断；
- PS 公屏仍只显示简短的动作比较，不显示隐藏 particle 配置。

## 实战范围

冻结后只做私服观察测试：

1. 先做 1 局；
2. 检查日志和延迟；
3. 再做 2 局；
4. 不用 1 至 3 局判断胜率；
5. 不在本版本同时引入动态 belief rollout。

## 尚未包含

本版本没有在单次假想 rollout 内执行：

```text
动作对
-> 完整 battle state transition
-> 公开事件生成
-> 双方信息集更新
-> belief 更新
-> 下一层 policy 调用
```

这属于下一个 `Belief-Aware One-Step Rollout` 版本。
