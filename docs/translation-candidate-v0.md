# Gen 9 Random Battle 转译候选版本 v0

## 结论

当前 PS protocol -> `battle-state/v1` -> `live_reference` 链路已经通过代码、
单元测试、离线 replay 和修复后的在线 smoke 准出条件，可以继续用于 8093
shadow reference 和 8094 DualView 小规模实战。

这不是 `8093` 与 `8095` 动作逐回合完全一致的声明，也不是 Dual 性能冻结。
动作差异仍需使用同一 state snapshot 单独分析。

## 角色

- `8095`：直接运行 untouched upstream Foul Play，实际执行动作；
- `8093`：接收同一对局的 adapter 转译状态，只做 reference/shadow；
- `8094`：DualView 实际决策服务。

## 本轮修复

### Baton Pass / Shed Tail

修复了 adapter 在检查保留 volatile 时只查找小写字段的问题。
PS protocol 可能产生 `Substitute` 这样的大小写形式，因此现在使用归一化
effect id 判断。

当前语义与 Foul Play `battle_modifier` 对齐：

- Baton Pass 保留 boosts；
- Baton Pass 保留 Substitute；
- Baton Pass 保留 Leech Seed；
- Shed Tail 只保留 Substitute；
- 普通换人不保留 boosts 或 volatile；
- 新宝可梦的 `substitute_hit` 从 false 开始。

## 自动化测试

### pokemon-battle-ai

通过：

```text
PS adapter tests passed
PS decision bridge tests passed
upstream protocol mirror tests passed
PS protocol replay tests passed
live observer tests passed
PS client tests passed
decision router tests passed
rules provider tests passed
node --check adapter.js
node --check decision-bridge.js
node --check live-observer.js
```

新增或强化的 fixture 包括：

- Baton Pass；
- Shed Tail；
- Substitute；
- 普通换人状态清理；
- Wish；
- Future Sight；
- Healing Wish；
- volatile alias；
- Encore/Taunt duration。

### foul-play-lab

```text
581 passed
Ruff: All checks passed
```

新增了 `sideEffectDetails` 到 Foul Play 的 Wish、Future Sight、Healing Wish、
Baton Pass 和 Shed Tail 映射测试。

## 离线 replay parity

使用既有 20 局 upstream replay 重新通过当前 adapter 和 decision bridge：

```text
比较决策点：677
state 成功匹配：677/677
missing state：0
reference error：0
无字段差异：658
有字段差异：19
boost-only 差异：0
```

此前报告中的大量 `*-boosts` 差异需要更正。对
`battle-gen9randombattle-1379:4` 复核发现：

```text
T1: Arbok 的 Intimidate
    -> |-unboost|p2a: Volbeat|atk|1
T2: Volbeat 换下，Tyranitar 上场
```

原 adapter 只清除了新上场宝可梦的状态，没有清除被换下的 Volbeat 状态，
因此 reserve 中错误保留了 `atk-1`。Foul Play 在换人时会清除被换下宝可梦的
boosts 和 volatile。倒下宝可梦的类似残留也属于同一类高风险问题。

现已修复：

- 普通换人清除旧 active 的 boosts、volatile、duration 和
  `movesUsedSinceSwitchIn`；
- Baton Pass / Shed Tail 的保留内容先复制到新 active，再清理旧对象；
- 继续保持 Baton Pass/Shed Tail 的保留规则不变。

当前 19 条差异已经按来源归类：
- Zoroark-Hisui/Espeon：Illusion 的公开视图与 upstream 内部真实物种差异；
- Minior-Meteor/Minior-Orange：canonical form alias；
- active/team moves：request 和 PS protocol 比 upstream summary 更完整；
- 少量 HP 或屏障持续回合：upstream summary 与 request 时点边界，需要保留为
  已知审计项，不能批量删除真实状态。

未发现新的未分类核心差异。

## 在线 live shadow smoke

运行目录：

```text
D:\Other\ai\pokemon-battle-experiments\dual-view-v0\upstream-20261005-021141
```

连续 3 局 `8095 upstream Foul Play` 对远程 `FoulPlayBot`，同时由 `8093`
做 translated shadow：

```text
完成对局：3
比较点：91
reference error：0
缺失 reference action：0
缺失 actual action：0
reference selection fallback：0
state diff 记录：36
state diff 非 boost-only 记录：0
```

修复后重新完成 3 局在线 smoke，未再出现 `boosts` 残留：

```text
完成对局：3
比较点：88
reference error：0
缺失 reference action：0
缺失 actual action：0
reference selection fallback：0
state diff：2
boost-only diff：0
```

剩余 2 条差异均可解释：

- `side-condition-auroraveil`：Light Clay 屏障在 request 时点存在一回合
  计数边界；
- `active-me-moves` / `team-garchomp-moves`：upstream summary 只保留当前
  active 已使用或可见的动作，而 adapter 保留完整 request move slots。

没有出现新的 active、HP、status、boosts、非法动作或 reference error。

3 局均正常结束，没有因 adapter 或 reference 导致的超时、非法动作或中途
退出。

## 已知限制

以下内容仍不应被描述为完整 upstream battle modifier 等价：

- Illusion 的内部真实配置与公开视图；
- 部分复杂 duration 的 request 时点边界；
- damage/miss/fail/immune 对隐藏配置 belief 的完整推断；
- 极少数 upstream summary 的 reserve HP、moves、boosts 摘要差异。

这些限制已从“未知转译错误”中分离出来。若后续发现 live 对局出现
active、HP、status、move、side condition 或 terrain/weather 的非 boost-only
差异，应暂停 Dual 批量测试并重新打开 parity 审计。

## 当前准出状态

```text
Translation Candidate v0: 通过
Translation Stable: 未宣称
Dual performance freeze: 未宣称
```

下一步可以在不改变 8093/8095 校准职责的前提下，恢复 8094 DualView 的
小规模对比测试。
