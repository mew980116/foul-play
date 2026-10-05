# 8093 与 8095 性能校准记录

更新时间：2026-10-05

## 当前结论：撤回转译准出，暂不冻结

本次扩展到实际 engine 字段后发现，之前的摘要 parity 无法覆盖
stats/boosts 键名、Tera 协议和 Encore 等错误。不能继续把“摘要无未解释差异”
当作“8093 能代表原版”的证据。以下旧批次均未加载本次修复。

### 实测数据

| 指标 | 修复前 8093 执行 | 新一批 8095 upstream 执行 |
|---|---:|---:|
| 完成局数 | 20 | 20 |
| 胜负 | 3–17 | 11–9 |
| 平均对局回合 | 29.95 | 31.00 |
| 我方最终存活 | 0.25 | 1.95 |
| 我方非首发换入次数 | 8.65 | 8.00 |
| 我方攻击招使用次数 | 12.85 | 14.60 |
| 我方总出招次数 | 22.50 | 24.00 |
| 我方 Tera 次数 | 0.75 | 0.95 |
| 我方首次 Tera 回合，仅已使用样本 | 12.07（15 局） | 18.74（19 局） |
| 平均记录决策耗时 | 249.16 ms | 1177.33 ms |
| P50 / P95 | 248 / 267 ms | 1166.28 / 1276.93 ms |

耗时不等于纯 engine 搜索时间：8095 计时包围原版 `async_pick_move`，
8093 是 runner 记录的请求决策耗时；不能据此认定算法快了多少倍。
P95 使用 nearest-rank 重算，旧文中的 266 ms 使用了不同取位方式。
批次顺序执行、队伍和轨迹独立，不是配对实验，也不能由此量化修复收益。

新一批 8095 的 697 个决策点与旧 8093 shadow：341 同、356 不同、
reference error 0。这个一致率只属于旧代码，不能用于候选版准出。

完整统计：
`D:\Other\ai\pokemon-battle-experiments\dual-view-v0\reference-performance-summary-1791183260072.json`

### 本轮修复与已知缺口

候选 `ps-adapter-state-sync/v0.4.1` 已修复：

- request stats 映射为 Foul Play 完整键名，并保留私有准确 HP；
- sampled nature/EV 应用，以及对手 boosts 的 engine 键名；
- 换人、濒死、Haze 和定向 clearboost 的能力等级清理；
- 形态变化后 public HP/boosts 对象匹配，以及双方同名对象隔离；
- 真实 `|-terastallize|` 事件和 request Tera 标记；
- Encore last-move 形态名匹配；输入不完整时明确报错，避免 Rust panic；
- 对手视角的我方 HP 只保留公开百分比；
- belief filter 按物种匹配，避免 active-first 重排改变 particle 归属。

已知未解决：1492 局 T41–44 的 6 个决策点中，Ditto 的 Transform
复制了目标能力、招式、属性和 boosts；仅采样 Ditto 原配置是不正确的。
候选版现明确返回 `unsupported-live-transform`，而不是默默传错状态。
这只是安全隔离，不是功能修复，仍阻止冻结。

扩展审计结果：

- 旧 781 点：所审字段无差异、无转换异常；
- 新 697 点：691 点所审字段无差异，6 点明确不支持 Transform；
- faint 后 request 清空 Tera，而 engine 保留全队已用历史，属于已解释差异；
- 审计不证明完整协议语义、全部状态字段和搜索策略等价。

证据：
`D:\Other\ai\pokemon-battle-experiments\dual-view-v0\engine-input-postfix-old-20261005-145507.json`
`D:\Other\ai\pokemon-battle-experiments\dual-view-v0\engine-input-postfix-new-20261005-145526.json`

另对新批次等间隔抽取 100 点，用候选 policy 的 100 ms 配置离线重新决策：
99 点成功，1 点因已知 Transform 显式拒绝；成功点无动作投影，输入未被修改。
这不是实战 smoke，也不是与 upstream 同随机源的策略一致性实验。

`D:\Other\ai\pokemon-battle-experiments\dual-view-v0\reference-policy-postfix-20261005-145653.json`

### 部署状态与下一步

停止旧 8093 进程以及启动独立 8096 的操作都被执行策略拒绝。
因此 8093/8094 仍运行旧代码，8096 未启动，修复后实战尚未开始。

接下来先实现并验证 Transform 的事件时刻配置复制，再启动候选服务，
连续 3 局 smoke；检查服务错误、普通动作投影、重复无效招和换人循环。
通过后才进行新的 20 局，并分别保存修复前后的结果。

### Transition 状态

旧手写伤害预览未忠实覆盖随机事件和状态推进，默认已阻止用于 rollout。
旧 500 点审计的“basic gate pass”和“零泄漏”只反映有限探针，撤回准出解释。
现有 poke-engine binding 的 native instructions 已接入离线转移包装，
无需改 engine；但 engine state 到公开事件、信息集和后验的链路尚未完成，
不能将当前 scaffold 称为已实现 belief rollout，也未接入 8094。

## 以下为旧批次原始记录（口径已修正）

## 测试对象

```text
8095：untouched upstream Foul Play，实际执行动作
8093：translated Foul Play reference，实际执行动作
远程对手：FoulPlayBot
格式：Gen 9 Random Battle
搜索预算：100 ms
```

## 结果

8093 单独执行 20 局：

```text
3 胜 17 负
胜率：15%
决策点：703
非法动作：0
动作投影：48（raw-best-action-not-legal）
请求/服务故障：1（runner 中计为 2 条 fallback）
决策延迟平均：249.16 ms
决策延迟 P50：248 ms
决策延迟 P95：266 ms
```

其中 2 条 fallback 来自同一次故障的 error 与后续回退记录。
服务日志已定位为 Encore 缺 last move 导致 Rust panic，并非已证实的网络故障。
这与另行统计的 48 次动作投影是不同口径，不能合并或互相替代。

8095 untouched upstream 的既有 20 局校准：

```text
10 胜 10 负
```

8093 的 20 局与 8095 的 20 局不是同一组随机队伍和同一条对局轨迹，
因此不能直接给出因果性能结论。当前结果只能标记为：

```text
8093 相对 8095 存在明显负向信号，需要继续定位；
不能证明全部负向差异来自 adapter 转译。
```

## 与状态 parity 的关系

同场景 8095/8093 影子比较中：

```text
决策点：781
reference error：0
旧摘要层未解释的差异：0（未覆盖实际 engine 字段，不能作为准出）
```

因此当前需要并行处理两条问题：

1. 继续审查 policy/search/selector 差异；
2. 修复 8093 实际执行中的服务稳定性和可能的动作质量偏差。

在这两个问题没有进一步收敛前，不把 8093 的 15% 胜率当作 Dual
算法性能结论，也不将 8093/8095 的动作一致率直接解释为转译通过率。
