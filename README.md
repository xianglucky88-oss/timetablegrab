# timetablegrab — 抓取/下载多源 GTFS 类时刻表，解析、换版差分、线路对齐与结构校验。

难度：**困难**　|　子方向：数据采集 / 爬虫与 RPA　|　运行栈：Python 3.14 + 标准库 `sqlite3` + `httpx` + `pytest`（离线，无需联网）

## 基础版代码（起点，能跑）
读 `fixtures/input.json` → `loader` → `parse` → 其余已实现变换 → `keyer` → 幂等落 `sqlite`。
```
make run     # 跑通 happy-path，打印 {fed, rows}
make test    # happy-path + 幂等 + 缺口桩断言
```

## 单一职责约定
每个模块 = 一个文件 = 一个聚焦函数；`pipeline.py` 只做顺序编排、不含业务判断。
业务模块：`feed_pull, parse, diff, align, validate, version` ＋ 通用 `loader / parse / keyer / store / pipeline / cli`。

## 故意留空（功能开发面）
- `diff.py` → `process()` 抛 `NotImplementedError`（对记录做单一职责变换：diff；功能开发目标，未接入 happy-path）
- `validate.py` → `process()` 抛 `NotImplementedError`（对记录做单一职责变换：validate；功能开发目标，未接入 happy-path）

## 朴素实现短板（功能优化面）
- `version.py` → 朴素实现 + docstring 标注短板（对记录做单一职责变换：version；功能优化目标）
- `parse.py` → 朴素实现 + docstring 标注短板（对记录做单一职责变换：parse；功能优化目标）

> 缺口模块刻意未接入 happy-path，以保证基线可运行；它们正是出题要补的 0→1 / 1→n 功能。
