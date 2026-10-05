# Foul Play 与 Dual PS adapter 状态一致性审计

日期：2026-10-04

## 结论先行

原版 Foul Play 不是只保存“当前宝可梦、HP 和招式”。它有两层状态：

1. `Battle` / `Battler` / `Pokemon` 的持久战斗状态；
2. `battle_modifier.process_battle_updates()` 对 PS protocol 的逐事件语义重放。

原版 `process_battle_updates()` 当前登记了 48 类 protocol action handler，随后
`async_update_battle()` 在收到 `request` 时结束上一批事件，`run_battle.py` 对
`Battle` 做 `deepcopy`，再调用 `user.update_from_request_json()` 和
`find_best_move()`。

Dual 目前的链路是：

```text
PS protocol
  -> pokemon-battle-ai/platform/ps/adapter.js
  -> decision-bridge.js battle-state/v1
  -> foul-play-lab/fp/live_reference.py
  -> 手动构造 Foul Play Battle
  -> Foul Play oracle / Dual root reweighting
```

因此 Dual 复用了 Foul Play 的搜索和 engine 边界，但没有复用原版
`battle_modifier` 的完整语义状态机。此前出现的场地状态、层数、active 映射和
重复招式问题，根因就是这层 converter 不是完整 replay。

本审计的判断是：

- 8093 与 8094 可以比较同一个 `battle-state/v1` 快照下的 policy；
- 8093 不能被称为 untouched upstream online Foul Play；
- 在状态 parity smoke 通过以前，不应把 Dual/8093 差异解释为策略差异；
- 当前应优先完善 adapter 和 converter，不调整 Dual 权重。

## 原版状态入口

### 请求 JSON

原版 `Battler.update_from_request_json()` 会处理：

- `rqid`；
- `forceSwitch`、`wait`；
- 当前 active；
- active 的 `trapped` / `maybeTrapped`；
- active 的完整招式列表；
- 每个招式的 `disabled`、PP、Z move 能力；
- `canDynamax`、`canTerastallize` 等能力；
- 队伍成员的 slot、details、level、nickname；
- condition、HP、status、fainted/reviving；
- item、ability、stats；
- reserve 中已知招式。

### Protocol replay

原版 `process_battle_updates()` 的 handler 集合包括：

```text
switch, faint, -fail, drag,
-heal, -damage, -sethp,
move,
-setboost, -boost, -unboost, -status,
-activate, -anim, -prepare,
-start, -singlemove, -end,
-curestatus, -cureteam,
-weather,
-fieldstart, -fieldend,
-sidestart, -sideend, -swapsideconditions,
-item, -enditem, -immune, -ability,
detailschange, replace, -formechange, -transform,
-mega, -terastallize, -zpower,
-clearnegativeboost, -clearboost, -clearallboost,
-singleturn, -mustrecharge,
upkeep, cant, inactive, inactiveoff, turn, noinit
```

这些 handler 不只是记录文本，还会更新：

- active/reserve；
- 普通换人时的 boosts、volatile、毒伤计数、PP 和 switch-in 限制；
- Baton Pass / Shed Tail 的保留状态；
- last used move；
- item、ability、forme、Tera；
- status、boosts、substitute 和 volatile duration；
- weather、terrain、Trick Room、Gravity 的持续回合；
- side conditions 及层数；
- Choice lock、Taunt、locked move、Fake Out/First Impression 限制；
- damage/speed/ability 证据导致的随机配置约束。

## 字段 parity matrix

状态列含义：

- `已同步`：当前 adapter 和 live converter 都有明确路径；
- `部分同步`：有字段或协议记录，但语义不完整；
- `缺失`：当前不会可靠传给 Foul Play；
- `仅原版推断`：原版会根据战报做额外推断，Dual 当前没有等价实现。

| 状态类别 | 原版 Foul Play | Node adapter / battle-state | live_reference | 当前判断 |
|---|---|---|---|---|
| battle id / turn / format / gen | `Battle` 基础字段 | 有 | 有 | 已同步 |
| rqid / request JSON | 保存并用于更新 | 保留完整 `request` | 已同步 `rqid`，完整 request 继续使用 | 已同步 |
| wait | `Battle.wait` | request 保留 | 已同步 | 已同步 |
| force switch | `Battle.force_switch` | `pending` 和 request 保留 | 已同步到 `battle.force_switch` | 已同步 |
| active / reserve | switch、drag、request 共同维护 | 有 active/team/slot | 有 self private 和 sampled opponent | 已同步，需继续覆盖 forced-switch 边界 |
| slot / ident / nickname | 参与 reserve 匹配 | ident、slot 有，nickname 未单独建模 | 主要按 name/details 映射 | 部分同步 |
| HP / max HP / status | request、damage、heal、sethp | 有 | 有 | 已同步 |
| fainted / reviving | request、faint | fainted 有，reviving 已保留 | 已映射 request 字段和 HP | 已同步 |
| stat boosts | boost/unboost/clear 系列 | 有并限制在 [-6, 6] | 有名称转换 | 已同步 |
| 普通 volatile | `-start` / `-end` | active volatile 字典 | active volatile 字段可映射 | 部分同步 |
| volatile duration | 原版维护 duration | 当前只保存可见 duration，很多为 unknown | 有字段入口，但无法从所有 PS 行恢复 | 部分同步 |
| Substitute HP / 是否被击中 | 原版维护 `substitute_hit` | 当前没有可靠的 substitute HP | 未可靠映射 | 缺失 |
| Encore / Taunt / locked move | 原版锁招和 duration | 有协议 volatile，但缺少完整 duration/来源语义 | 只做有限 volatile 映射 | 部分同步 |
| confusion / yawn / partially trapped | 原版有专门 duration 和清理逻辑 | 只保留一般 volatile | 未完整映射 | 缺失 |
| last used move | move/cant/switch 更新 | 已加入 side/Pokemon last move | 已从 history 映射 | 已同步基础路径 |
| last selected move | 原版在真正发送前写入 | 已由 `recordSelectedAction()` 记录 | 已传给 converter | 已同步基础路径 |
| moves known | 原版按 request 和公开 move 更新 | 有 | 有 | 已同步 |
| move slot | 原版 request 顺序保留 | state 以原 slot 输出 | Foul Play move object 按 request 顺序建立 | 已同步 |
| move disabled | 原版 request 直接设置 | `moveDetails` 保留，state 合法动作过滤 | move object 保留 disabled | 已同步 |
| move PP | 原版 request 设置 current PP | `moveDetails` 保留 | active move 设置 current PP | 已同步 |
| Choice item lock | 原版依赖 item + last used move | item 可见，锁定由 converter 补 | last move 已有，完整推断仍有限 | 部分同步 |
| Fake Out / First Impression | 原版依赖 switch 后 move history | moves-since-switch 已记录 | 当前有 history 禁用逻辑 | 已同步基础限制 |
| Assault Vest | 原版按 item 禁 status move | item 可见 | 当前未统一在 converter 中补锁 | 缺失 |
| weather | 原版更新类型、source、duration | 当前 weather 有，source/duration 入口已加 | 类型已映射，duration 缺失时为 unknown | 部分同步 |
| terrain | 原版更新类型和 duration | fieldState 有 terrain | 类型已映射，duration 多为 unknown | 部分同步 |
| Trick Room | 原版独立 bool 和 duration | fieldState 有 | 有入口，但持续回合仍不完整 | 部分同步 |
| Gravity | 原版独立 bool | fieldState 有 | 有入口 | 部分同步 |
| Stealth Rock | 原版 side condition 计数 | 有 | 有，且达到上限会禁 setup | 已同步 |
| Spikes 层数 | 原版整数层数 | 由 history 恢复 | 有 | 已同步 |
| Toxic Spikes 层数 | 原版整数层数 | 由 history 恢复 | 有 | 已同步 |
| Sticky Web | 原版 side condition | 有 | 有 | 已同步 |
| Reflect / Light Screen / Aurora Veil | 原版带 duration | 有 screens，并在设置时记录持续时长 | 有存在性和已知 duration；仍需覆盖更多边界 | 部分同步 |
| Safeguard / Mist / Tailwind | 原版带 duration | 有 screens/side conditions | 有存在性，duration 不完整 | 部分同步 |
| Toxic count | 原版每回合维护 | 从公开 `-damage ... tox`、换人和 curestatus 重建 | 已传入 `sideConditionDetails.toxic_count` | 部分同步 |
| Wish / Future Sight / Healing Wish | 原版 side 级别维护 | adapter 未结构化保存 | 未传入 | 缺失 |
| Baton Pass / Shed Tail | 原版保留特定 boosts/volatile | history 可见但未语义化 | 未完整实现 | 缺失 |
| item / ability | 原版 set/remove/inference | 公开 protocol 和 request 有 | self private / opponent revealed 有 | 部分同步 |
| forme / transform / illusion | 原版会重建 Pokemon | 有基础字段和公开 details | 主要按 details/name 映射 | 部分同步 |
| Tera | 原版记录 Tera type 和状态 | 有 | 有 history 映射 | 已同步 |
| Dynamax / Mega / Z move | 原版有对应 handler | 部分字段 | Gen 9 只重点覆盖 Tera | Gen 9 之外暂不冻结 |
| damage / miss / fail / immune | 原版用于状态和 set 推断 | 大多只保留 history | converter 不重放结果语义 | 仅原版推断 |
| speed / damage / ability 证据 | 原版更新 dataset possibilities | history 有 | 未完整迁移 | 仅原版推断 |
| battle time remaining | 原版 `inactive` 更新 | 未进入 decision state | 未使用 | 缺失，非 Gen9 policy 核心 |

## 本轮已完成的基础设施修复

本轮没有修改 baseline、poke-engine、PS package-lock 或既有 benchmark 结果，已修改
以下集成层文件：

- `D:\Other\ai\pokemon-battle-ai\platform\ps\adapter.js`
  - 保存 active request 的 disabled、PP、move slot；
  - 记录 side/Pokemon 的 last used move；
  - 记录 last selected move；
  - 保存 volatile、field、weather、side-condition detail；
  - 记录 force switch、Trick Room、Gravity 和 terrain 基础状态；
  - 在设置屏障时记录 Light Clay 等道具带来的持续时长，避免设置者换下后被错误缩短；
  - 修复 request move 与 protocol move 的大小写重复问题。
- `D:\Other\ai\pokemon-battle-ai\platform\ps\decision-bridge.js`
  - 将 field/weather、last selected/used move、volatile 和 moveDetails 带入
    `battle-state/v1`。
- `D:\Other\ai\foul-play-lab\fp\live_reference.py`
  - 保留 disabled move 和 current PP，而不是把 disabled move 从 Foul Play Battle
    删除；
  - 同步 `force_switch`、`rqid`、`wait`；
  - 从公开 history 恢复 switch/move 的 last used move；
  - 接收 last selected move；
  - 映射基础 volatile、fainted/reviving、weather/field state。
- `D:\Other\ai\foul-play-lab\tests\test_live_reference.py`
  - 增加 disabled/PP、force switch、last selected/last used move 的回归测试。

## 当前仍然必须补的基础设施

优先级按会直接造成“错误招式/错误换人”的风险排序：

1. `Assault Vest`、Choice lock、Encore、Taunt、locked move 的统一 converter；
2. Substitute HP、Yawn、confusion、partially trapped 的 duration；
3. weather/terrain/screens 的更多准确 duration 边界；
4. Wish、Future Sight、Healing Wish；
5. Baton Pass / Shed Tail 的保留规则；
6. `fail`、`cant`、`immune`、damage roll 等证据进入一致性过滤；
7. 以真实 replay fixture 做 adapter state 与 Foul Play state 的逐事件 parity diff。

## 准出门槛

在重新跑 100 局前，至少要满足：

- adapter、decision bridge、protocol replay 测试全通过；
- Foul Play lab 全量测试全通过；
- 关键 fixture 对 `Spikes`/`Toxic Spikes` 层数、force switch、PP、disabled、
  Tera、weather/terrain、volatile、last move 无差异；
- 不出现 Dual-only 的 `fail/cant`；
- 不出现当前场地已有时重复布置 hazard；
- 不出现已明确失效的 Fake Out、First Impression、Sleep Talk、Yawn、No Retreat；
- 每个 `8093/8094` diff 都能在同一 state snapshot 上复现；
- 完成以上条件后，才重新建立新的 5 局、20 局和 100 局性能批次。
