# 8093 Foul Play Reference Contract v0.1

更新时间：2026-10-05

## 1. 角色

8093 是 translated Foul Play reference sidecar：

```text
PS protocol
    -> pokemon-battle-ai adapter
    -> battle-state/v1
    -> Foul Play battle conversion
    -> poke-engine MCTS
```

8093 不执行动作。它只返回 reference policy，供以下用途使用：

- 检查 PS protocol 转译是否可靠；
- 对照 8095 untouched upstream Foul Play；
- 对照 8094 DualView；
- 保存每个决策点的 policy 和搜索诊断。

8095 仍然是原始 Foul Play 的实际执行入口，不能由 8093 替代。

## 2. Endpoint

```text
GET  http://127.0.0.1:8093/health
POST http://127.0.0.1:8093/reference
POST http://127.0.0.1:8093/choice
```

`/choice` 与 `/reference` 使用同一套 reference policy。8093 的响应不会向 PS
发送命令。

## 3. Reference mode

### diagnostic

用于单状态复现和转译问题定位：

- 固定 particle 数；
- 固定 search budget；
- 固定 seed；
- 直接选择 aggregate policy 的 argmax；
- 不模拟 upstream 的动态 world 数和近最优随机 selector。

默认启动示例：

```powershell
python tools/live_reference_server.py --port 8093 --particles 1 `
  --search-time-ms 50 --seed 17 --mode diagnostic
```

### upstream-compatible

用于 8095 行为校准：

- 早期、公开信息较少且对手尚未公开招式时使用 `4 * parallelism` 个 world；
- 其他阶段使用 `2 * parallelism` 个 world；
- 早期 world 使用一半搜索时间；
- 对 aggregate policy 应用 upstream 的 `0.75 * top_probability` 候选阈值；
- 在候选集合中按 policy 权重选择动作；
- 使用 battle id、rqid、turn 和 seed 生成可复现 selector 随机源；
- 输出每个 world 的诊断信息。

当前模式是 upstream-compatible，而不是声明与 upstream 的随机状态逐字节相同。
它的目的，是消除“固定单粒子 + argmax”这一额外比较变量。

启动示例：

```powershell
python tools/live_reference_server.py --port 8093 --particles 0 `
  --search-time-ms 100 --search-parallelism 1 --seed 17 `
  --mode upstream-compatible
```

`--particles 0` 表示启用动态 world 数；大于 0 时表示固定粒子数，主要用于
诊断或消融。

## 4. 响应字段

核心字段：

| 字段 | 含义 |
|---|---|
| `schemaVersion` | `foul-play-reference/v1` |
| `referenceContractVersion` | `reference-contract/v0.1` |
| `translationVersion` | adapter/state conversion 版本 |
| `bestAction` | 当前服务最终选择并交给合法动作投影的动作 |
| `rawBestAction` | aggregate policy 的 argmax 动作 |
| `selectedAction` | 合法动作投影后的动作 |
| `actions` | aggregate root policy |
| `selectionMode` | `argmax` 或 upstream-compatible selector |
| `selectionCandidates` | upstream-compatible selector 的候选动作及概率 |
| `particleCount` | 本次实际使用的 world 数 |
| `dynamicSampling` | 是否按 upstream 规则动态确定 world 数 |
| `searchTimePerWorldMs` | 每个 world 的实际搜索预算 |
| `worldDiagnostics` | 每个 world 的 particle、访问次数和 root policy |
| `selectionFallback` | 是否发生合法动作投影 |
| `selectionFallbackReason` | 投影原因 |

`bestAction`、`rawBestAction`、`selectedAction` 必须分别保留，不能互相覆盖。

## 5. 8093 与 8095 的准出条件

### 输入层

- reference error 为 0；
- 非法动作投影为 0，forced-switch fallback 单独统计；
- active、HP、faint、status、boost、volatile、moves、weather、terrain、hazards、
  screens、Tera 和公开队伍成员没有未解释的决策相关差异。

### Policy 层

- world 数和每-world budget 可从日志解释；
- root action universe 和 Top-3 policy 可比较；
- selector 差异不再与 adapter 差异混在一起。

### State parity 的分类

当前 parity 分为：

- `translationDifferences`：影响 translated state 的差异；
- `baselineSummaryDifferences`：upstream baseline 摘要与 PS 历史快照的差异，
  不直接归咎于 adapter；
- `opponentPublicDifferences`：对手公开信息差异；
- `selfDifferences`：我方完整状态差异。

屏障剩余回合在两边采样时刻相差一回合时，记录为
`baseline-side-condition-*-timing`，不作为转译错误。

## 6. 当前校准结果

最近一次 20 局 upstream-compatible 校准：

```text
目录：
D:\Other\ai\pokemon-battle-experiments\dual-view-v0\upstream-20261005-120432

决策点：781
8095 胜负：10 - 10
reference error：0
普通 selection fallback：13
动态 world：4 个 66 次，2 个 715 次
最终动作一致：388 / 781 = 49.68%
```

重新按当前 parity 分类器审查这批状态：

```text
translationDifferences：
    仅剩 Aurora Veil 剩余回合采样时机差异 3 次

baselineSummaryDifferences：
    Ho-Oh HP 摘要差异 1 次
    Cyclizar HP 摘要差异 7 次
```

因此当前可以认为：

> 转译层中此前的公开形态、公开 HP、Transform boost、队伍成员匹配等主要
> 差异已经被修复或重新分类；剩余 3 次属于状态快照时间差异，仍需保留诊断，
> 但不能继续作为已确认的 adapter 错误。

动作一致率仍然不是胜率或转译正确率。upstream-compatible selector 会主动
从近最优动作中随机选择，因此最终动作一致率可能低于 diagnostic argmax
比较。

## 7. Dual control

8094 支持：

```text
--mode upstream-compatible
--strength 0
--opponent-exploration 0
```

该配置返回 `dual-control-upstream-compatible`，用于确认 8094 的 shared
self-policy 是否与 8093 reference 接近。只有 control 基本通过后，8094
完整 Dual 与 8093 的差异才可归因于 belief、opponent prediction、
action-pair evaluator 或 root reweighting。

