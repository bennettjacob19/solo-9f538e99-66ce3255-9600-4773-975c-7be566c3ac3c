# 专线改道演练服务（Circuit Migration Rehearsal）

用 FastAPI 实现的改道演练服务：提交有向链路与容量、2～10 条隧道的带宽、以及每条隧道**起终点相同的现用路径和目标路径**，服务返回演练号与一条能全程走通的改道顺序；无解则拒绝并说明原因，不建立演练。

演练期间可为隧道**登记备路**（保护路径）：提交备路与确认改道时，服务用拟生效的现用路和备路**逐条模拟单链路中断**——两条专线平时能共用一条备路，改道后却可能因同一断点同时切到备路而超载，这类隐患在核查阶段就会被拦下。

## 启动

```bash
docker compose up --build
```

打开 <http://localhost:8000/docs> 即可在 Swagger 页面提交备路、确认改道与查看。数据落在命名卷 `rehearsal-data` 的 SQLite 文件中，容器重启后演练状态（含已登记备路）仍在。

本地直接运行：

```bash
pip install -r requirements.txt
uvicorn app.main:app --reload        # DB_PATH 默认 data/migrations.db
python -m pytest                     # 57 个测试
```

## 计费规则

* 全部容量、带宽均为**整数 Mbps**。
* 每条隧道的现用路径持续占用带宽，直到该隧道**确认切换**为止。
* 目标路径与现用路径**共有的链路只计一次**：预留时只对「目标路径独有」的链路扣带宽，切换时旧路独有链路的带宽自动释放。
* 一条路径内同一链路重复出现会被拒绝（避免自环与重复计费）。

## 接口

| 方法 | 路径 | 作用 |
| --- | --- | --- |
| `POST` | `/rehearsals` | 提交演练。成功 `201`，返回 `rehearsal_id` 与 `order`/`steps`；无解 `409 no_feasible_order`（不创建）；结构非法 `422 invalid_scenario` |
| `GET` | `/rehearsals/{id}` | 查看现用路径、每条隧道的 `backup_path`，以及每条链路的 `used` / `reserved` / `available` |
| `PUT` | `/rehearsals/{id}/backups` | **整组提交备路**（隧道号 + 有向链路列表），空列表或空请求体即清空；核查不过 `409 failover_unsafe`，结构非法 `422 invalid_backup`，链路无端点 `409 backup_unsupported` |
| `POST` | `/rehearsals/{id}/reservations` | 为**顺序中的下一条**隧道预留带宽，返回预留编号；同一时刻只允许一笔待确认预留 |
| `POST` | `/rehearsals/{id}/reservations/{rid}/confirm` | 凭编号确认：核查通过后切换路径并释放旧路独有带宽；核查不过 `409 failover_unsafe`，预留保持待确认 |
| `POST` | `/rehearsals/{id}/reservations/{rid}/cancel` | 凭编号取消：只撤回本次预留，旧路不动，该步可重做 |
| `GET` | `/health` | 存活探针 |

### 状态约束

* **一次一笔**：已有待确认预留时，再发起预留返回 `409 step_conflict`，状态不变。
* **按序执行**：请求非「顺序中的下一条」隧道返回 `409 step_conflict`，状态不变。省略 `tunnel_id` 即自动取下一条。
* **编号一次性**：未知、已确认、已取消、不属于本次演练的编号，确认/取消均返回 `409 reservation_invalid`，状态不变。
* **取消可重做**：取消后演练回到该步之前，重新预留会得到新编号。
* 全部隧道切换后演练状态为 `completed`，再预留返回 `409`。

## 备路与单链路中断核查

两条专线平时各走各的现用路，可以共用同一条备路；但改道之后，两条隧道可能位于同一断点之后，断点一断会**同时**切到这条备路。因此备路不是登记了事，而是在两个时机做核查：

* **提交备路时**（`PUT /rehearsals/{id}/backups`）：仅支持**链路端点齐全**的演练。每条备路须连续、起终点与现用路一致；整组替换，空提交即清空。
* **确认改道时**（`confirm`）：用**拟生效**的现用路（该隧道已切到目标路径）和已登记备路核查。

核查方式是对每条链路逐一模拟单链路中断：

* 未受影响的隧道留在现用路；
* 受影响的隧道改走备路，**没有登记备路的停流**（不视为错误）；
* 故障情景下**未确认的预留视为撤回**，只占现用路/备路的带宽；
* 受影响的隧道有备路、但**备路也经过断点**，或**未断链路超容量** → 核查失败。

核查失败返回 `409 failover_unsafe`，指出中断的链路，并给出失去备路的隧道、超载链路与带宽缺口：

```json
{
  "error": "failover_unsafe",
  "detail": "if link 'L1' fails: overloaded links: 'L4' short by 2 Mbps (12/10 Mbps); ...",
  "failures": [
    {"link_id": "L1", "tunnels_without_viable_backup": [],
     "overloaded_links": [{"link_id": "L4", "capacity": 10, "load": 12, "gap": 2}]}
  ]
}
```

**核查通过才保存备路或切换路径**；确认失败后预留仍是待确认状态，可凭原编号取消（或清除备路后重试）。共用同一 SQLite 文件的多个实例也按提交顺序核查：所有变更在锁内校验、按文档版本号 compare-and-set 提交，冲突方重载最新状态后重新核查，因此并发的「提交备路」与「确认改道」不会同时放行一对互相冲突的状态。

演练查询（`GET /rehearsals/{id}`）中每条隧道带 `backup_path` 显示已登记备路；功能上线前创建的旧演练没有该字段，默认视为未登记（`null`）。

## 一次完整演练

```bash
BASE=http://localhost:8000
ID=$(curl -s -X POST $BASE/rehearsals -H 'Content-Type: application/json' \
       -d @examples/failover.json | python3 -c 'import sys,json;print(json.load(sys.stdin)["rehearsal_id"])')

# 0) 整组登记备路（T1、T2 都把 L4 当备路；空请求体即清空）
curl -s -X PUT $BASE/rehearsals/$ID/backups -H 'Content-Type: application/json' -d @examples/backups.json

# 1) 预留 → 2) 确认；确认前会再做一次单链路中断核查。重复到全部完成
RES=$(curl -s -X POST $BASE/rehearsals/$ID/reservations | python3 -c 'import sys,json;print(json.load(sys.stdin)["reservation_id"])')
curl -s -X POST $BASE/rehearsals/$ID/reservations/$RES/confirm
curl -s $BASE/rehearsals/$ID       # 查看各链路已用/预留带宽与每条隧道的 backup_path
```

`examples/failover.json` 中 T1、T2 平时分居不同现用路，L4 任一条都能单独承接；若把 L4 容量改成 10，T1 改道后两条隧道同处 L1/L2 之后，确认时就会因「L1 断 → 同时切 L4 → 12 > 10」被 `409 failover_unsafe` 拦下。`examples/infeasible.json` 是一个环形死锁（每条目标链路都被现用路径占满），提交会返回 `409` 且不建立演练。

## 负载格式

```json
{
  "links":    [{"link_id": "L1", "source": "A", "target": "B", "capacity": 10}],
  "tunnels":  [{"tunnel_id": "T1", "bandwidth": 4,
                "current_path": ["L1"], "target_path": ["L2"]}]
}
```

备路提交：

```json
{"backups": [{"tunnel_id": "T1", "path": ["L3", "L4"]}]}
```

* `source`/`target` 可选：全部给出时会校验路径连续性、以及两条路径起终点一致；全部省略时仅按链路 id 计费（此类演练不能登记备路）。
* 兼容常见别名：`id`/`from`/`to`/`path`/`new_path`/`edges`/`circuits` 等。
* 错误响应统一为 `{"error": "<code>", "detail": "...", "errors": [...]}`；`failover_unsafe` 另带 `failures` 明细。

## 实现要点

* `app/planner.py`：状态只取决于"哪些隧道已切换"，因此在 2^n（n≤10，至多 1024）个子集上做带失败记忆的 DFS，**精确**判定有无可行顺序并给出顺序；有随机场景与穷举对拍的测试。
* `app/failover.py`：对每条链路重放单链路中断——未受影响的留现用路、受影响的切备路、无备路停流、未确认预留不计；检出"备路同过断点"与"未断链路超载（含缺口）"。
* `app/engine.py`：所有状态变更（含备路提交）在锁内完成，并以文档版本号做 compare-and-set 提交；并发或共用 SQLite 文件的多实例下，冲突方按提交顺序重载重查，只会有一笔预留/一次确认/一次备路提交生效。
* `app/store.py`：每次状态变更写一条 JSON 文档（SQLite 或内存），不会出现半更新；备路存在文档的 `backups` 字段，旧文档缺省为空。

