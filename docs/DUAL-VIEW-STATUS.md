# Dual-view 开发状态

截至 2026-10-03，第一阶段目标限定为 Gen 9 Random Battle。

## 已完成

### 1. PS protocol replay

集成仓库中的 `platform/ps/protocol-replay.js` 可以离线重放：

- 原始 PS protocol；
- benchmark JSON row；
- JSON array；
- JSONL protocol log。

现有 benchmark 完成对局的批量检查结果：

- 728 场完成对局；
- 728/728 replay 正常结束；
- 728/728 winner 与 benchmark 记录一致；
- 事件数无差异。

### 2. DualViewState

`fp/dual_view.py` 当前提供：

- `PublicBattleState`；
- `PublicPokemonState`；
- `PrivatePokemonState`；
- `BeliefParticle`；
- `DualViewState`。

`DualViewState` 明确区分：

- `public`：双方都能从公开协议重建的信息；
- `self_private`：我方真实私有配置；
- `belief_about_opponent`：我方对对手配置的候选；
- `belief_about_us`：对手可能持有的我方配置假设。

`opponent_visible_input()` 不包含我方私有字段和任何 belief。

### 3. Gen 9 Random Battle belief sampler

`fp/belief_sampler.py` 中的 `Gen9RandomBattleBeliefSampler`：

- 只接收 `PublicBattleState`；
- 不读取 request 或真实 private state；
- 使用当前 pinned Foul Play Random Battle dataset；
- 已揭示成员按合法 set 的 dataset count 加权采样；
- 未揭示成员按 dataset 和基础 Random Battle 队伍约束补全；
- 支持固定 seed；
- 输出六只完整配置组成的 `BeliefParticle`。

当前粒子权重在采样后均匀化。该实现是可重复的先验候选生成器，不是完整联合
Bayesian posterior。

### 4. 一致性过滤

`Gen9RandomBattleBeliefSampler.filter_consistent_particles()` 只使用公开信息过滤
粒子：

- 已观察的 species；
- 已观察的 moves；
- 已观察的 item、ability、Tera type；
- `PublicBattleState.history` 中的公开 `move` 事件。

过滤后会重新归一化粒子权重。当前没有加入 damage roll、speed 区间、隐藏能力触发
或更复杂的联合队伍约束，因此仍属于第一版 consistency gate。

### 5. Foul Play offline policy oracle

`fp/policy_oracle.py` 中的 `FoulPlayPolicyOracle`：

- 接收完整 Foul Play `Battle`；
- 支持 `self` 和 `opponent` 两种 perspective；
- 对 `opponent` perspective 使用 `battle_to_poke_engine_state(..., swap=True)`；
- 调用当前 pinned `poke_engine.monte_carlo_tree_search()`；
- 返回每个 action 的 `visits`、`total_score`、`probability` 以及总访问次数；
- 使用 `deepcopy`，不会修改调用方传入的 `Battle`；
- 不连接 PS，也不改变线上 decision bridge。

### 6. Shadow prediction

`fp/shadow_prediction.py` 中的 `FoulPlayShadowPredictor`：

- 接收我方视角下的完整 `Battle` 和 `belief_about_us` particles；
- 将每个 particle 作为对手眼中的我方假设配置；
- 对每个假设调用 opponent-perspective policy oracle；
- 按 particle weight 聚合对手动作分布；
- 输出动作概率、每个 particle 的 oracle 结果和归一化权重。

当前 shadow prediction 是离线实验接口，不是完整二阶 MCTS，也没有接入线上决策
路径。对手动作观测后的 posterior 更新尚未实现。

### 7. Root-action reweighting

`fp/root_reweighting.py` 中的 `RootActionReweighter`：

- 接收我方 policy oracle 的根节点动作分布；
- 接收 shadow prediction 的对手动作分布；
- 通过调用方提供的 `(our_action, opponent_action) -> value` evaluator 计算期望价值；
- 用 `log(prior) + strength * expected_value` 进行稳定的 softmax 重加权；
- 不假设 poke-engine 已提供完整的 action-pair value API；
- 不修改 Foul Play 的在线 `find_best_move()` 和 decision bridge。

这一步是离线 evaluator，不是完整二阶 MCTS。`action_value` 目前必须由实验调用方
明确提供，避免把未经验证的规则或引擎近似混入线上路径。

### 8. Replay decision observer

`tools/replay_observer.py` 可以读取主集成仓库保存的 benchmark `matches.jsonl`，
按 `matchId` 和 `decision-index` 选择一个真实 replay 决策点，并输出：

- 当前公开队伍和 active；
- replay 中记录的实际动作；
- 根据公开信息生成的 Gen 9 Random Battle belief particle；
- Foul Play self policy；
- opponent shadow prediction。

当前 benchmark protocol 没有保存原始私有 request JSON，因此 observer 使用
“公开 replay + 采样配置”重建 Foul Play `Battle`。输出会明确标注这一限制，不把
采样配置当成真实私有队伍。

示例报告：

`D:\Other\ai\pokemon-battle-experiments\reports\replay-observer-sample.json`

当前 root reweighting 在 observer 中仍未计算，因为还没有经过 replay 验证的
action-pair value evaluator。

## 验证结果

2026-10-03：

- Foul Play lab 全量 pytest：`537 passed`；
- ruff check：通过；
- ruff format check：通过；
- Node replay / adapter / client / decision bridge / benchmark stats / rules provider：
  全部通过。

## 已知限制

当前 sampler 还没有：

- 基于 damage/speed/ability 证据的高级 joint consistency filter；
- 对手动作观测后的策略条件更新；
- 根节点动作重加权。
- 真实 replay 到 DualViewState 的自动转换；
- Foul Play oracle 与实际 online decision bridge 集成；
- 线上 shadow 运行；
- 线上胜率验证。

下一步应为 root evaluator 接入可由 replay 验证的 action-pair value 来源，然后
再考虑 shadow-only 的线上观测；Rust engine、线上 PS 连接和线上胜率验证仍保持不变。
