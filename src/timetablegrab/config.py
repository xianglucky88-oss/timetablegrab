"""单一职责：集中管理路径、资源上限与对齐阈值（不含业务判断）。"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "fixtures" / "input.json"
DB_PATH = ROOT / "timetablegrab.db"

# 原始下载、条件缓存、隔离区与报告目录（每个下载尝试使用独立新空目录）
DATA_DIR = ROOT / "var" / "feeds"
CACHE_DIR = ROOT / "var" / "cache"
QUARANTINE_DIR = ROOT / "var" / "quarantine"
REPORT_DIR = ROOT / "var" / "reports"

# ---- 资源限制（取舍说明见 README“资源限制与坏数据容忍度”）----
DOWNLOAD_TIMEOUT_SECONDS = 30.0
MAX_SHARD_BYTES = 50 * 1024 * 1024          # 单分片 50 MiB
MAX_BUNDLE_BYTES = 200 * 1024 * 1024        # 整包 200 MiB
MAX_ROWS_PER_SHARD = 200_000                # 单分片行数上限
MAX_FINDINGS_PER_REPORT = 2_000             # 报告条目上限，避免坏文件刷爆存储
MAX_DOWNLOAD_ATTEMPTS = 3

# ---- 跨源对齐阈值 ----
ALIGN_HIGH = 0.85        # >= 自动应用
ALIGN_MEDIUM = 0.60      # >= 自动应用（保留依据）
ALIGN_AMBIGUITY_GAP = 0.05   # 最佳与次佳差距小于该值视为歧义，降为低置信
COORD_NEAR_M = 50.0
COORD_MID_M = 200.0
COORD_FAR_M = 1000.0

# ---- 差分阈值 ----
FULL_REWRITE_RATIO = 0.8  # 无对齐依据且删增占比超过该值才判定“整表重写”

# 校验严重级别
BLOCKER, WARNING, REPAIRED = "BLOCKER", "WARNING", "REPAIRED"

# 版本状态机
ST_DOWNLOADING = "downloading"
ST_REJECTED = "rejected"
ST_STAGED = "staged"
ST_ACTIVE = "active"
ST_SUPERSEDED = "superseded"
ST_WITHDRAWN = "withdrawn"
ST_CANCELED = "canceled"
ST_STALE = "stale"
