# DualView 信念规划路线与 reference 固化方案

更新时间：2026-10-05

## 1. 目标与范围

本项目的长期目标不是用一个全新的搜索器替换 Foul Play，而是把 Foul Play
从“完整确定化状态上的策略搜索器”逐步变成：

- 战斗规则和伤害模拟器；
- 完整世界上的 action/value oracle；
- belief-aware 规划器的叶节点求值器。

第一阶段仍只覆盖 Gen 9 Random Battle。`D:\Other\ai\foul-play` 是 untouched
baseline，`D:\Other\ai\foul-play-lab` 负责实验算法，`D:\Other\ai\pokemon-battle-ai`
负责 PS protocol、adapter、reference 和线上集成。

## 2. 从文本中记录的后续算法路线

文本提出的主路线是：

```text
根节点 belief 加权
    -> 一层 belief rollout
    -> MO-ISMCTS / POMCP 式搜索
    -> 关键节点局部贝叶斯矩阵求解
    -> 小型抽象上的 CFR / Bayesian-CFR 对照
    -> belief policy network 作为后期加速层
```

需要保留以下判断：

1. belief 不是替代搜索，而是改变搜索节点的决策对象；
2. 普通“每个完整世界独立 MCTS”容易出现 strategy fusion；
3. 未来的节点应携带公开历史、当前私有状态和与历史一致的隐藏状态粒子；
4. 双方都有私有信息，因此不能直接把单智能体 POMCP 原样套用；
5. 全局 CFR 不适合作为当前第一版，先用局部抽象和小规模对照；
6. 规则模拟、belief 更新、对手策略建模和策略求解应保持分层。

## 3. 基于当前代码库的由简到难路线

### P0：基础设施和 reference 固化

目标：先证明输入可靠，并得到可以解释的原始 Foul Play 对照。

- 完成 PS protocol -> adapter -> `battle-state/v1` 的状态审计；
- 修复和归类 active、HP、status、boost、volatile、moves、PP、天气、场地、
  hazards、screens、Tera、faint 和换人差异；
- 保留一个 deterministic diagnostic reference，用于复现单个状态；
- 新增 upstream-compatible reference，用于和 8095 做行为校准；
- 记录 sampled world 数量、每个 world 搜索预算、root policy、selector 候选集
  和最终动作；
- 不把最终动作一致率直接当作转译正确率。

P0 的准出条件是“决策相关 state diff 为零或有明确解释”，而不是胜率相等。

### P1：根节点 belief 加权

这是当前 DualView-v0 的工作层级：

```text
公开历史
    -> 配置粒子
    -> 每个完整世界调用 Foul Play oracle
    -> root policy 按 belief 权重聚合
    -> 对手 shadow prediction
    -> root action reweighting
```

当前 `ActionPairEvaluator` 仍然是静态一回合评估器，不推进完整状态，也不在
假想动作后更新 belief。它应继续作为消融基线，不应命名为完整二阶 MCTS。

P1 的主要工作：

- 让 reference 和 Dual 共享 policy aggregation、动作命名和合法动作投影；
- 将 action-pair value 与状态转换错误分离；
- 记录对手预测 Top-1、Top-3、校准概率和实际动作；
- 为 `strength=0`、`opponent_exploration=0` 等消融提供稳定开关。

### P2：一层 belief rollout

目标：让候选动作的价值不仅来自静态动作对评估，而是来自一次真实的动作和
观测推进：

```text
我方候选动作 + 对手候选动作
    -> 推进完整 battle state
    -> 生成公开观测
    -> 按观测过滤或重采样双方 belief
    -> 在下一根节点调用 Foul Play oracle
```

第一版只做一层或两回合 horizon，不修改 poke-engine 核心搜索。

### P3：局部贝叶斯矩阵求解

在局部动作集合上形成：

```text
V(a_our, a_opp | belief, random outcome)
```

再比较：

- 对当前 opponent model 的最佳响应；
- maximin；
- 受约束混合策略；
- upstream Foul Play 的确定化策略。

先限制在关键节点和小动作集合，不求全局精确解。

### P4：MO-ISMCTS + DUCT 原型

目标是让不同隐藏世界在同一个 information-set 节点共享统计，缓解
strategy fusion。

最低可行原型：

- 我方信息集树；
- 对手信息集树；
- 动作后按公开 observation 分叉；
- 每方只使用自身信息集可获得的统计；
- 先做短 horizon 和少量粒子。

这一步需要重新设计 tree key、belief 生命周期和观测分支，复杂度明显高于
当前外围包装，因此不应在 P0/P1 未稳定时提前做。

### P5：局部 CFR / Bayesian-CFR 对照

只在抽象后的典型局面使用：

- 开局两到三只已知局面；
- 强化与反强化局面；
- 换人与攻击局面；
- 残局两到三只局面。

用途是估计局部混合策略和 exploitability，作为 MO-ISMCTS、矩阵求解器和
Foul Play 策略的科学对照，不作为线上第一版主决策器。

### P6：belief policy network

最后才考虑把稳定的 belief-search 结果蒸馏为：

```text
policy(public_state, private_state, belief_summary)
value(public_state, private_state, belief_summary)
```

它是运行时加速层，不应替代早期可审计的 belief sampler、reference 和
规则模拟链路。

## 4. 算法选择的总体技术路线

推荐的长期分层架构：

```text
PS protocol / replay
        |
        v
状态转译与可见性边界
        |
        +--> 8093 reference：原始 Foul Play-compatible policy
        |
        v
贝叶斯粒子滤波
        |
        +--> 对手配置后验
        +--> 对手对我方配置的后验
        +--> 对手策略类型后验
        |
        v
局部矩阵求解 / 一层 belief rollout
        |
        v
MO-ISMCTS 或 POMCP 风格 information-set search
        |
        v
Foul Play / poke-engine 完整世界模拟和叶节点求值
```

其中：

- POMCP 是 belief tree 的参考方向，但当前是双边私有信息游戏，不能直接照搬；
- MO-ISMCTS 更适合作为保留 Foul Play 搜索结构的中期方向；
- 局部贝叶斯矩阵是当前最值得先做的策略层升级；
- CFR 主要用于局部抽象和验证；
- policy network 只在数据和评估链成熟后用于加速。

## 5. 8093 与 8094 的职责和预期差异

### 8093 不应被定义为“Dual 的简化版”

8093 的职责是：

```text
同一份 translated battle-state
    -> 尽可能复现 untouched upstream Foul Play
    -> 不执行动作
    -> 输出可审计的 policy 和诊断数据
```

当前 8093 的 `1 particle + 1 次 MCTS + argmax` 是 diagnostic 配置，不是因为
Dual 必须这样做，也不是最终 reference 定义。

### 8093 应拆成两个模式

#### Diagnostic mode

```text
固定 seed
固定 particle 数
固定 search budget
固定 argmax
```

用途是单状态重放、回归测试和转译问题定位。

#### Upstream-compatible mode

应尽量复刻 `D:\Other\ai\foul-play` 中的：

- Random Battle 动态 sampled world 数；
- per-world search budget；
- root policy 聚合；
- 75% 近最优动作筛选；
- 最终动作选择器；
- 随机数种子和诊断记录。

8093 默认作为线上 shadow 对照前，必须先通过这个模式的离线校准。不能只
因为最终动作一致率提高，就宣称状态转译正确；仍需保留逐字段 state parity。

### 8094 的差异边界

8094 与 8093 应共享：

- PS adapter；
- `battle-state/v1`；
- battle state 转换；
- Foul Play/poke-engine 边界；
- action naming 和合法动作投影；
- 基础 self-policy aggregation。

8094 额外拥有：

- 对手视角的我方配置 belief；
- 对手 action prediction；
- 我方候选动作与对手动作的联合评估；
- root reweighting；
- 后续的 belief update 和 rollout。

因此，8093 和 8094 的差异应尽量集中在：

```text
belief-aware strategy layer
```

而不应分叉在：

```text
protocol translation
engine state conversion
basic action legality
```

## 6. 尽早固化 8093 的方案

### 6.1 先固化接口，不先固化“胜率”

8093 的第一版 reference contract 应固定返回：

- `referenceMode`；
- `bestAction`；
- `rawBestAction`；
- `selectedAction`；
- `actions`；
- `sampleCount`；
- `searchTimePerWorldMs`；
- 每个 sampled world 的 root policy 摘要；
- `selectionCandidates`；
- `selectionFallback`；
- `stateParity` 诊断；
- `translationVersion`。

### 6.2 8093 与 8095 的准出条件

分三层判断：

1. **输入层**：决策相关字段没有未解释的 state diff；
2. **policy 层**：相同输入下 root action universe、Top-3 分布和访问量趋势
   可解释；
3. **selector 层**：使用 upstream-compatible selector 后，最终动作差异落在
   搜索随机性和采样方差范围内。

不能把“每回合完全同动作”作为唯一条件，也不能只看 20 局胜负。

### 6.3 8093 与 8094 的公平比较

正式比较 Dual 前，需要提供一个明确的 control：

```text
DualViewPolicy(
    dual_strength=0,
    opponent_exploration=0,
    compatible self-policy settings
)
```

这个 control 应尽可能退化为与 8093 相同的 self policy。然后比较：

```text
control 8094 vs 8093
full 8094 Dual vs 8093
```

如果 control 与 8093 仍大量不同，先修 shared policy/translation；如果 control
基本一致而 full Dual 不同，差异才可以归因于 belief、opponent model 或
root reweighting。

## 7. 近期执行顺序

截至 2026-10-05，下一批任务按以下顺序执行：

1. 保留当前未提交改动和实验结果，继续定位 8095/8093 的公开 HP、公开队伍
   成员等 state diff；
2. 给 8093 增加 Diagnostic/Upstream-compatible 两种模式；
3. 将 upstream 的动态采样和最终 selector 诊断化；
4. 用对齐条件重新跑小批量 20 局；
5. 固化 8093 reference contract 和 parity 报告；
6. 给 8094 增加 shared-self-policy control；
7. 做 `8094 control vs 8093`；
8. 再做 `8094 Dual vs 8093`；
9. 只有以上链路稳定后，才进入一层 belief rollout；
10. 最后再考虑 MO-ISMCTS、局部 CFR 和策略网络。

## 8. 当前不应做的事情

- 不立即重写 poke-engine；
- 不立即做全局 CFR；
- 不把 8095 删除或改造成 HTTP sidecar；
- 不用远程 FoulPlayBot 胜率替代本地 policy parity；
- 不把静态 action-pair evaluator 叫作二阶 MCTS；
- 不在转译层未准出前用 8094 与 8095 的最终胜率推断 Dual 优劣。

