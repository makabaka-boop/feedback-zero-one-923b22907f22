# 采集记录资格裁定服务（Eligibility Service）

纯后端、零第三方依赖（Python 标准库 + SQLite），提供版本化数据维护、申请受理、
资格裁定、结果持久化与中断恢复。

---

## 1. 运行说明

环境要求：Python ≥ 3.10（已用 3.11 验证），无需安装任何包。

```bash
cd /workspace

# 可选：初始化数据库（其余命令会按需自动建库，init 是幂等操作）
python3 -m eligibility_service --db data.sqlite init

# 新增（原子批量）
python3 -m eligibility_service --db data.sqlite add-measurements --json '[
  {"identifier":"m1","subject":"alice","collected_at":100,"payload":{"v":1}}]'
python3 -m eligibility_service --db data.sqlite add-associations --json '[
  {"identifier":"e1","subject_a":"alice","subject_b":"bob"}]'
python3 -m eligibility_service --db data.sqlite add-authorizations --json '[
  {"identifier":"a1","subject":"bob","purpose":"p1","decision":"allow","start_at":0,"end_at":200}]'

# 导出申请（同一标识 + 同一用途重试即返回原申请当前状态）
python3 -m eligibility_service --db data.sqlite export --purpose p1 --application-id app1

# 查询申请状态
python3 -m eligibility_service --db data.sqlite query --application-id app1

# 恢复（进程重启后由任意命令自动续办；也可显式驱动）
python3 -m eligibility_service --db data.sqlite resume-all
```

批量数据也可以用文件或标准输入提供：`--file batch.json`、`--file -`。

### 退出码

| 码 | 含义 |
|----|------|
| 0 | 成功；返回完成态，或（续办/故障场景下）等待态 |
| 2 | 非法输入（标识、JSON、区间、版本号等） |
| 3 | 冲突（批内/已有重复标识、未知对象、未知申请、用途冲突） |
| 4 | 暂时性故障：申请仍为 `waiting`，重试即可续办 |
| 5 | 申请已终结为 `failed`（`reason` 给出原因） |
| 1 | 未预期的内部错误 |

失败时 stdout 无输出，stderr 输出 `{"error":{"code":...,"message":...,"details":...}}`。

### 测试与验收

```bash
python3 -m unittest discover -s tests -v      # 27 个实现级测试
python3 acceptance/blackbox_acceptance.py     # 109 项独立黑盒验收
```

独立验收脚本**不导入**服务代码，只通过子进程调用 CLI，覆盖资格裁定、版本隔离、
中断恢复（含 `os._exit` 硬崩溃、进程级并发）三大必验区域；`--keep` 可保留数据库。

---

## 2. 数据模型

| 对象 | 字段 |
|------|------|
| 采集记录 | `identifier`、`subject`、`collected_at`（整数时刻）、`payload`（任意 JSON） |
| 身份关联 | `identifier`、`subject_a`、`subject_b`（无向；允许自环与平行边） |
| 用途授权 | `identifier`、`subject`、`purpose`、`decision`（`allow`/`deny`）、`start_at`、`end_at`（非空左闭右开整数区间） |

规则：
- 标识与用途为 **非空 ASCII 字母数字串**（≤512 字符）；主体为任意非空文本
  （≤1024 字符），无需预先登记。
- 标识在**同类**现存对象内唯一；不同类之间互不干扰（同名测量与关联可以共存）。
  批内标识重复一律拒绝。
- `payload` 接受任意 JSON 值；`NaN/Infinity`、非整数时刻等被拒绝。
- 修订（新增 / 整体替换 / 删除）以版本为单位：成功即推进版本并返回新版本号。

## 3. 资格裁定规则（全部在受理版本上求值）

1. 以主体为节点、关联为无向边构造身份图；某记录的主体所在**连通分量**即其
   **成员集合**（关联平行边、自环不影响结果）。
2. 对采集时刻 `t`、用途 `p`，一条授权“适用”当且仅当：其主体属于成员集合、
   用途相同、且 `start_at <= t < end_at`。
3. 只要存在适用授权为 **允许或拒绝**，有拒绝即排除；否则至少一项允许才纳入；
   无适用授权排除。
4. 无向分量内从被授权成员出发的身份闭包就是整个分量，因此合格记录的成员集合
   恒为完整分量。

导出结果形态：

```json
{"application_id":"app1","accepted_version":4,"purpose":"p1","status":"completed",
 "result":{"version":4,"purpose":"p1",
   "records":[{"measurement":{"identifier":"m1","subject":"alice","collected_at":100,"payload":{}},
               "members":["alice","bob"],
               "authorizations":["a1"]}]}}
```

- 记录按测量标识、成员按主体名的 **ASCII 码序**排列；`authorizations` 为适用授权
  标识的 ASCII 排序去重集合。
- 不合并不同标识的同值记录；合法空集也是 `completed`。
- `waiting` 态不含 `result`；`failed` 态只含 `reason`，不含 `result`。

## 4. 一致性与恢复设计

* **版本化存储**：每行携带 `[valid_from, valid_to)` 版本区间；修订只关闭旧行并插入
  新行，从不改写历史行。因此任意已提交版本都可在日后完整重建，旧申请与后续修订
  天然隔离（即使中间重启）。
* **受理先落盘**：申请行（`waiting` + 受理版本）先提交，然后才构建结果；查询/重试
  总能找到原申请。结果与终态在同一事务内一次性写入（单条 UPDATE），读方不可能看到
  半成品。
* **串行等价**：单连接 + 线程锁；写事务用 `BEGIN IMMEDIATE`，进程间由 SQLite 串行化。
  构建阶段在写事务之外、基于 WAL 一致性快照读取固定版本，只有短暂的落盘持有写锁。
* **恢复**：`waiting` 是唯一可续办状态。进程启动（每个 CLI 命令都会打开服务）自动
  续办，另有 `resume` / `resume-all`。`synchronous=FULL` 保证提交在进程猝死后仍然存在。
* **终态不变**：`completed`/`failed` 不再参与续办，也永不重建；`failed` 只由确定性
  故障产生（数据无法解释、存储损坏、显式注入）。
* **错误分类**：数据库锁竞争/暂时不可用 → 保持 `waiting`（退出码 4）；存储数据确定
  无法解释 → 终结 `failed`（`invalid_stored_data`）。

## 5. 接口一览

| 命令 | 说明 |
|------|------|
| `init` | 建库/升级（幂等） |
| `add-measurements` / `add-associations` / `add-authorizations` | 原子批量新增（JSON 数组） |
| `replace-measurements` / `replace-associations` / `replace-authorizations` | 原子批量整体替换（按标识） |
| `delete-measurements` / `delete-associations` / `delete-authorizations` | 原子批量按标识删除 |
| `export --purpose P [--application-id ID]` | 受理（或重试）导出申请，返回当前状态 |
| `query --application-id ID [--purpose P]` | 查询申请；用途不符拒绝 |
| `resume --application-id ID` / `resume-all` | 显式续办等待中的申请 |
| `version` | 当前已提交版本 |
| `snapshot --version V --purpose P` | 诊断：在指定版本上重算裁定结果 |
| `dump --version V` | 诊断：导出指定版本的原始对象 |

诊断与故障注入选项（仅用于测试，不影响正常路径）：
`--fault STAGE`（注入暂时故障）、`--fail-build STAGE`（注入确定性构建故障）、
`--crash STAGE`（`os._exit` 硬崩溃）、`--no-resume`（打开时不自动续办）。

Python API：`eligibility_service.Store`、`EligibilityService`
（`add_*` / `replace_*` / `delete_*` / `accept` / `get_application` / `resume` /
`resume_pending`）以及 `build_result`。

## 6. 代码结构

```
eligibility_service/
  errors.py      错误类型与稳定错误码
  validation.py  纯校验（标识、主体、时刻、JSON 规范化、大小上限）
  store.py       SQLite 连接、模式、版本事务、一致性读快照
  dataset.py     版本化对象维护与版本内快照读取
  service.py     身份图 / 区间索引、裁定构建、申请生命周期与恢复
  cli.py         JSON 命令行接口
  __main__.py    python -m eligibility_service
tests/test_service.py            实现级测试
acceptance/blackbox_acceptance.py 独立黑盒验收
```

## 7. 已知边界

- 单个 `payload` 上限 1 MiB（规范化 UTF-8 字节）。
- `|collected_at|`、`|start_at|`、`|end_at|` ≤ 10^18。
- 结果整体以一条 JSON 文本持久化，超大结果集会使单行较大（30k 记录约 150 MB）；
  属于演示规模内的取舍。
- 关联删除/替换按标识整体生效，不提供“按边”独立版本化。
