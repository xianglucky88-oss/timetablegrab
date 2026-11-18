# timetablegrab — 多源时刻表下载、解析、跨表校验、站点对齐、换版差分与生效切换

运行栈：Python 3.11+ 标准库 `sqlite3`（离线）；在线下载可选依赖 `httpx`；测试 `pytest`。

## 运行方式

```bash
make run      # 遗留通用记录流水线（向后兼容，读 fixtures/input.json）
make test     # 运行测试（无 pytest 时可执行 python3 -I tests/_pytest_shim.py）
```

时刻表 CLI（离线文件源放在 `var/sources/<source>/manifest.json` + 分片）：

```bash
python3 -m timetablegrab.cli ingest <source>     # 下载→解析→校验→对齐→差分→登记/换版
python3 -m timetablegrab.cli versions <source>   # 版本状态机
python3 -m timetablegrab.cli activate <s> <token>
python3 -m timetablegrab.cli withdraw <s> <token> [reason]
python3 -m timetablegrab.cli query <table> [--token T | --date YYYY-MM-DD]
python3 -m timetablegrab.cli recover <source>    # 中断后状态收敛
```

## 包格式与完整性门禁

每个来源的整包由 `manifest.json` 与若干分片组成；清单声明 `version_token`、
`effective_date`、每个分片的 `size` 与 `sha256`。下载阶段（`feed_pull.py`）规则：

- **每次下载尝试使用独立新空目录** `var/feeds/<source>/<token>/attempt-N/`；
  原始分片原样保留，损坏分片移入 `var/quarantine/` 并附原因文件。
- **条件缓存只复用完整内容**：缓存按内容哈希寻址，仅当磁盘文件的大小与哈希
  同时匹配清单才复用。缺分片、截断、哈希不符、超限一律不写缓存、不标 COMPLETE。
- **可恢复日志** `journal.json` 原子落盘记录每个分片状态；中断后重入复用同一
  尝试目录，已验真分片不重复下载，重试达 `MAX_DOWNLOAD_ATTEMPTS` 仍残缺则阻断。
- **陈旧响应拒绝**：分片/清单响应令牌与清单 `version_token` 不一致（旧响应晚到）
  直接判 `PullError`，残缺版本永远不会进入激活。

## 解析与行定位（`parse.py`）

支持 JSON 数组与 JSONL；每条 `Row` 保留 `source_file` 与 `line` 依据。
JSONL 单行损坏、JSON 整体失败、非对象行、行数超 `MAX_ROWS_PER_SHARD` 均为
BLOCKER，分片 `ok=False` —— 局部解析失败不能当作成功，下游不得激活该版本。

## 校验界线（`validate.py`）

| 级别 | 含义 | 典型规则 |
| --- | --- | --- |
| BLOCKER | 不可自动修复，整包不得生效 | 重复键、外键悬空、停靠顺序倒退、非法时刻/序列、服务日超出日历窗口、日历窗口倒置 |
| WARNING | 可生效但保留人工复核依据 | 跨日班次、坐标缺失/越界、星期位图不符、未引用站点、未绑定日历 |
| REPAIRED | 确定性安全自动修复，原值留痕 | id/名称去空白、删除连续重复停靠并重排序列、非法星期位图回退每日服务 |

跨日班次采用 GTFS 风格 `HH:MM:SS`（小时可 ≥24），按出发日归属服务日期，
只告警不阻断。校验报告与规范化后的表数据一并入库（`validation_reports`）。

## 跨源对齐与 ID 漂移（`align.py`）

站点编号整体更换不直接算“删除+新增”。综合四类证据打分：
名称 0.45、坐标 0.35、方向 0.10、服务日期 0.10（缺证据取中性分，不惩罚）。

- `≥0.85` 且无歧义 → `exact`；`≥0.60` 且无歧义 → `fuzzy`；二者才进入自动 ID 映射。
- 最佳/次佳分差 `<0.05` 视为歧义，不自动配对；低于阈值记 `none` 并保留依据。
- 同名族一对多（人民广场 → 人民广场东/西）识别为 `split`，**保留依据但不自动迁 ID**。
- 每条对应都带名称、坐标距离、方向、日期重叠等可复核证据。

## 换版差分（`diff.py`）

经对齐映射配对的站点编号变化计入 `id_drift` 与字段级 `changed`，而非删增；
线路/班次在 id 变化时用名称+站点序列自然键回退配对。只有在**无任何对齐依据且
删增占比 ≥0.8** 时才判定 `full_rewrite`，避免“换了全部站点编号”被误报成整表重写。

## 版本状态机与生效切换（`version.py` + `store.py`）

状态：`staged → active → superseded`，旁路 `rejected / canceled / withdrawn`。

- 只有完整下载且校验无 BLOCKER 才能登记 `staged`；阻断版本登记 `rejected` 并留依据。
- **未来版**（生效日晚于当前日期）保持 `staged`，到日由 `activate_due` 激活；
  手动提前激活抛 `VersionConflict`；回溯生效日同样拒绝。
- **同日冲突**：同一生效日先入者占位，后来版本置 `canceled` 并写日志。
- **撤回**：未生效版本直接 `withdrawn`；已生效版本撤回后回退到最近历史版本，
  无历史版本时查询返回空而非跨版本拼接。
- 激活切换在**单事务**内完成（旧 active→superseded，新 staged→active）并写
  `activation_journal`；`recover()` 依据日志把中断的 activating/withdrawing
  收敛到确定状态。
- **查询严格版本隔离**：`query_*` 只能指定 token 或按日期取当时应生效版本，
  绝不拼接两个版本的数据；历史版本与历史差分长期可读。

## 持久化与依据保留

SQLite 表：`feed_versions`、`activation_journal`、`version_tables`、`version_diffs`、
`validation_reports`、`alignment_reports`、`raw_archives`（原始分片+哈希），
以及遗留 `records` 表（旧接口 `upsert/count/all_records` 与 `pipeline.run()` 语义不变）。

## 取舍说明：对齐可信度、坏数据容忍度、资源限制

- **对齐可信度**：高阈值（0.85）保证自动换 ID 仅在名称与坐标都强一致时发生；
  宁可把模糊对应留给人工（`none/split` 带依据），也不接受错误合并两个物理站。
  代价是换版后部分站点需要人工确认映射，自动化率让位于安全性。
- **坏数据容忍度**：结构性错误（重复键、外键、顺序、日期窗口）零容忍、阻断生效；
  格式性瑕疵（空白、连续重复停靠、星期位图）确定性自动修复并留痕；语义存疑
  （跨日、坐标缺失、未引用）告警放行。容忍度逐级显式化，避免“静默吞错”。
- **资源限制**（`config.py`）：单分片 50 MiB、整包 200 MiB、单分片 20 万行、
  报告 2000 条、下载 3 次重试。超限按 BLOCKER 处理，防止坏文件耗尽磁盘/内存；
  阈值集中配置，可按运营环境调整，取舍是“先保护流程稳定性，再谈吞吐”。

## 模块边界

每个业务阶段单一职责；`pipeline.py` 只做顺序编排，不含业务判断。
业务模块：`feed_pull`（下载）、`parse`（解析）、`validate`（校验）、
`align`（对齐）、`diff`（差分）、`version`（状态机）；
通用模块：`loader / keyer / store / models / hashutil / config / cli`。
