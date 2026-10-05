# DualView 冻结候选版 v0.3-state-sync

日期：2026-10-04

## 冻结范围

本版本只冻结以下内容：

- Gen 9 Random Battle；
- PS live state 到 Foul Play `Battle` 的状态转换；
- 当前 active Pokemon 的公开形态和换人映射；
- 我方当前 HP、状态和 stat boosts 的同步；
- Foul Play self/opponent oracle；
- 初版 opponent shadow prediction；
- 初版静态 action-pair root reweighting；
- `8093` reference sidecar 和 `8094` DualView decision service。
- benchmark 技术统计中的钛晶次数与首次钛晶回合统计。

本版本不冻结以下内容：

- 线上胜率；
- 第二阶 MCTS；
- 完整 action-pair state transition；
- Bayesian joint posterior；
- Gen 8 Factory；
- poke-engine 核心修改。

## 已修复的主要状态 bug

### 对手 active 错位

旧逻辑默认使用 `oppTeam[0]` 作为当前出战者。对手换人后可能继续把旧 active
的招式集合交给 opponent oracle。

当前逻辑优先使用 `state.opp` 和公开 switch history，并按 species/form 映射
公开 HP、状态、能力和已知招式。

### stat boost 名称不一致

PS 使用 `spa+6` 等短名称，Foul Play 使用 `special-attack` 等内部名称。
当前转换层已显式完成两套名称之间的映射。

### 我方队伍顺序变化

强制换人和 request 更新后，`myTeam` 的顺序可能与最初 request 不同。
当前私有 HP、状态和 boost 按 Pokemon 名称匹配，而不是按数组索引匹配。

### 公开形态

对于 `Goodra` / `Goodra-Hisui`、`Necrozma` / `Necrozma-Dusk-Mane`
等 PS 状态名和公开 details 不完全一致的情况，当前转换优先保留公开 switch
details 中的形态。

## 验证结果

- Foul Play lab Python tests：初始冻结时 `559 passed`；当前修复后
  `573 passed`；
- Ruff：`All checks passed`；
- PS adapter、client、protocol replay、live observer、battle stats tests：通过；
- Room 803 全部保存决策状态转换审计：
  - 2621 条决策状态；
  - active mapping mismatch：0；
  - boost mismatch：0；
  - conversion error：0；
- 100 场保存对局的首个决策状态接口 smoke：
  - 8093 reference error：0；
  - 8094 DualView error：0；
  - 无空 best action；
  - 无过期 Garganacl 动作预测；
  - 无动作映射错误；
- 8093 和 8094 当前均可用。

## 20 局 live audit 结果

实验目录：

```text
D:\Other\ai\pokemon-battle-experiments\dual-view-v0\live-audit-20-20261004
```

- 对局：20；
- 胜负：4 胜、16 负，胜率 20%；
- 平均回合：24.3；
- 决策数：588；
- setup timeout：0；
- battle timeout：0；
- 错误：0；
- 实际非法动作：0；
- 与 8093 普通决策一致率：60.29%（293/486）；
- `reference error`：0；
- `selectionFallback`：只发现已单独记录的 forced-switch transition fallback；
- 明显状态同步异常：未发现。

这批数据只支持“状态同步和实战链路可用”的冻结结论，不支持
“Dual policy 胜率改善”的结论。Dual 与 8093 的差异仍需通过
`strength=1` 与 `strength=0` 的单变量消融继续定位。

### 钛晶统计

从同一批 20 局协议回算：

- Dual 我方平均钛晶次数：0.75；
- Dual 我方使用钛晶的对局数：15/20；
- Dual 我方钛晶使用率：75%；
- Dual 我方首次钛晶回合平均值：10.13；
- Dual 我方首次钛晶回合 p25/median/p75：4/7/17；
- FoulPlayBot 平均钛晶次数：0.90；
- FoulPlayBot 使用钛晶的对局数：18/20；
- FoulPlayBot 首次钛晶回合平均值：11.61。

`battle-stats.js` 同时保留每方的 `teraTurns` 列表，便于后续按回合和
对局复盘。没有使用钛晶的对局，其 `firstTeraTurn` 为 `null`，不会被
计入首次钛晶回合的分位数。

## 本地 Foul Play 与远程 FoulPlayBot 对照

为检查 `8095` 本地 Foul Play reference 是否可以直接作为远程
`FoulPlayBot` 的性能等价物，已完成 20 局 Gen 9 Random Battle：

```text
D:\Other\ai\pokemon-battle-experiments\dual-view-v0\baseline-foul-play-20-20261004
```

结果：

| 指标 | 本地 Foul Play | 远程 FoulPlayBot |
|---|---:|---:|
| 对局 | 20 | 20 |
| 胜率 | 0%（0/20） | 对照方 100%（20/20） |
| 平均回合 | 20.1 | 20.1 |
| 我方平均非首发换人 | 16.35 | 3.20 |
| 我方平均攻击招式次数 | 4.30 | 14.35 |
| 本地决策延迟平均/p50/p95 | 86.45/84/107 ms | 不适用 |

0/20 在真实胜率为 50% 的假设下出现的概率约为
`9.54e-7`，因此这不是可以解释为普通小样本波动的结果。更重要的是，
本地 Foul Play 的换人和攻击行为已经明显偏离远程 bot，说明当前 `8095`
并不能直接视为服务器 `FoulPlayBot` 的性能等价物。

本地服务是 `LiveReferencePolicy`：它使用我方完整 request，但对对手隐藏
配置进行采样；它还使用当前配置的 50 ms search budget 和 1 个 particle。
远程 bot 的实际版本、搜索预算、set 处理和运行时配置尚未被本地实验完全
证明相同。因此，在完成本地 reference policy 校准前，不能把 Dual 与
远程 FoulPlayBot 的胜率直接解释成纯粹的 Dual 改进或退化。

完整对比文件：

```text
D:\Other\ai\pokemon-battle-experiments\dual-view-v0\foul-play-vs-dual-comparison-20261004.json
```

## Root reweighting 消融

为区分初版 root reweighting 与基础状态同步的影响，已完成一批修复后有效的
`strength=0` 对照。对照目录为：

```text
D:\Other\ai\pokemon-battle-experiments\dual-view-v0\ablation-strength-0-postfix-20261004
```

| 指标 | `strength=1` 当前 Dual | `strength=0` 对照 |
|---|---:|---:|
| 对局 | 20 | 20 |
| 胜率 | 20%（4/20） | 10%（2/20） |
| 平均回合 | 24.3 | 27.1 |
| 普通决策一致率 | 60.29% | 62.92% |
| 我方平均最终存活 | 0.30 | 0.30 |
| 我方非首发换人 | 9.30 | 6.80 |
| 我方攻击招式次数 | 11.35 | 11.85 |
| 我方换人率/回合 | 0.40 | 0.25 |
| 我方攻击率/回合 | 0.47 | 0.44 |
| 对手 Top-1 预测准确率 | 32.40% | 33.20% |
| 对手 Top-3 预测准确率 | 62.02% | 62.45% |

两批对局使用独立 Random Battle 队伍，不能把这 20+20 局解释成严格的
因果性能结论。修复后 `strength=0` 没有复现“明显过度换人”，但这批样本
也没有显示胜率改善；当前仍不能冻结 policy performance。完整结果保存在：

```text
D:\Other\ai\pokemon-battle-experiments\dual-view-v0\ablation-comparison-20261004.json
```

### 对照过程中发现并修复的状态同步问题

有效消融之前，首批 `strength=0` 运行暴露了两个此前测试未覆盖的输入形态：

1. PS 状态中 `weatherState.turnsRemaining` 可以是显式 `null`。转换层原先只在
   字段缺失时使用 `-1`，字段存在但为 `null` 时会把 `None` 传入 poke-engine，
   导致决策服务返回 400；
2. Random Battle 的公开或采样配置可能暂时包含超过四个候选招式。旧的
   last-used-move 转换会把第五个及之后的招式映射成无效的 poke-engine
   move index，触发 `Invalid PokemonMoveIndex: 5`。

修复方式是：

- 将 `null`、非整数天气剩余回合统一归一化为 Foul Play 的
  未知天气持续时间哨兵 `-1`；
- 只把前四个招式传入 poke-engine，同时将不在前四个槽位中的历史招式
  转换为 `move:none`，不修改 Foul Play 原始招式列表。

修复前的首批运行暴露了如下输入形态：
修复后：

- 新增回归测试；
- Foul Play lab：`574 passed`；
- 修复后 smoke：1 局完成，`fallback=0`，`invalidActionCount=0`；
- 修复后 `strength=0` 批次：20 局完成，reference error：0，非法动作：0，
  fallback：0；
- 修复后服务日志不再出现 `Invalid PokemonMoveIndex` 或 traceback。

首批失败批次仍保留在：

```text
D:\Other\ai\pokemon-battle-experiments\dual-view-v0\ablation-strength-0-20261004
```

该批次不纳入性能比较；有效对照使用 `ablation-strength-0-fixed-20261004`。

Room 803 修复后关键回合的现象：

- Turn 2 以后 opponent top prediction 不再出现 `Salt Cure`；
- Manaphy 的 `+3`、`+6 SpA` 已被转换并传入 Foul Play；
- Turn 2、Turn 3、Turn 4 不再因为状态错位而继续重复 `Tail Glow`；
- Turn 4 对 1 HP Mienshao 会优先进入攻击或合理换人候选。

## 已知剩余限制

### 1. 8094 的 forced-switch fallback

保存日志中部分记录属于上一只 Pokemon 已经 faint、PS 正在等待强制换人的
中间状态。此时旧的 `bestAction` 可能是上一回合的攻击，服务会从当前合法
换人动作中重新选择，并将 `selectionFallback` 标记为 `true`。

这不是非法动作，但线上统计中应将其单独归类为：

```text
forced-switch selection fallback
```

不能和普通决策 fallback 混为一谈。

### 2. 形态别名不等于 bug

部分保存状态中 `state.opp.name` 是基础名，例如 `Goodra`，而公开 switch
details 是 `Goodra-Hisui`。当前映射审计应以公开 active form 和实际构造的
Foul Play active form 比较，而不能只比较 PS adapter 的基础名。

### 3. 静态重加权仍是初版

当前 action-pair evaluator 不推进完整 battle state，也不在假想动作后重新
采样 belief。它保留为 `DualView-v0` 消融基线，不应解释为完整二阶 MCTS。

## 实战建议

可以开始小规模私服观察，但暂不建议直接宣称胜率改善。建议顺序：

1. 先使用当前 8093/8094 观察 1 局；
2. 检查每回合 room 公告和日志中的：
   - 当前 active species/form；
   - opponent prediction top3；
   - selected action；
   - `selectionFallback`；
3. 再做 3 至 5 局 smoke test；
4. 确认没有新的状态错位后，另建一批全新 benchmark；
5. 不复用修复前的 100 场结果作为性能结论。
