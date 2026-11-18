"""单一职责：时刻表领域的数据结构与错误类型（仅声明，不含逻辑）。"""
from __future__ import annotations
from dataclasses import dataclass, field, asdict
from typing import Any


@dataclass
class ShardSpec:
    name: str
    size: int
    sha256: str


@dataclass
class Manifest:
    """来源清单：版本标识、生效日期、完整性依据（分片大小/哈希）。"""
    feed_source: str
    version_token: str
    effective_date: str          # ISO 日期 YYYY-MM-DD
    published_at: str            # ISO 时间戳
    shards: list[ShardSpec]
    kind: str = "json"           # json | jsonl
    target: str = ""             # 可选：本包对应的数据表 stops/routes/trips/...
    schema_version: int = 1

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Manifest":
        return cls(
            feed_source=d["feed_source"],
            version_token=d["version_token"],
            effective_date=d["effective_date"],
            published_at=d["published_at"],
            kind=d.get("kind", "json"),
            target=d.get("target", ""),
            schema_version=int(d.get("schema_version", 1)),
            shards=[ShardSpec(**s) for s in d.get("shards", [])],
        )


@dataclass
class Finding:
    """校验/解析发现的问题，保留行定位与处理界线。"""
    severity: str                # BLOCKER / WARNING / REPAIRED
    code: str
    message: str
    table: str = ""
    entity_id: str = ""
    file: str = ""
    line: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ValidationReport:
    feed_source: str
    version_token: str
    findings: list[Finding] = field(default_factory=list)
    repaired: list[Finding] = field(default_factory=list)

    @property
    def blocked(self) -> bool:
        return any(f.severity == "BLOCKER" for f in self.findings)

    @property
    def warnings(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == "WARNING"]

    def add(self, f: Finding) -> None:
        if f.severity == "REPAIRED":
            self.repaired.append(f)
        self.findings.append(f)

    def to_dict(self) -> dict[str, Any]:
        return {
            "feed_source": self.feed_source,
            "version_token": self.version_token,
            "blocked": self.blocked,
            "findings": [f.to_dict() for f in self.findings],
        }


@dataclass
class Row:
    """解析后的行：data 为业务字段，provenance 保留行定位依据。"""
    table: str
    data: dict[str, Any]
    source_file: str
    line: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"table": self.table, "data": self.data,
                "source_file": self.source_file, "line": self.line}


@dataclass
class ParseResult:
    rows: list[Row] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)
    shard: str = ""
    ok: bool = True              # False = 局部解析失败，缓存不得视为完整

    @property
    def blocked(self) -> bool:
        return any(f.severity == "BLOCKER" for f in self.findings)


@dataclass
class AlignItem:
    old_id: str
    new_id: str
    confidence: float
    method: str                  # exact | fuzzy | split | merge | none
    evidence: dict[str, Any] = field(default_factory=dict)


@dataclass
class AlignReport:
    feed_source: str
    old_version: str
    new_version: str
    items: list[AlignItem] = field(default_factory=list)

    def mapping(self) -> dict[str, str]:
        """仅高/中置信的一对一对应进入自动映射；低置信、拆分/合并不自动换 ID。"""
        return {i.old_id: i.new_id for i in self.items
                if i.method in ("exact", "fuzzy") and i.confidence >= 0.60}

    def to_dict(self) -> dict[str, Any]:
        return {
            "feed_source": self.feed_source,
            "old_version": self.old_version,
            "new_version": self.new_version,
            "items": [asdict(i) for i in self.items],
        }


@dataclass
class DiffReport:
    feed_source: str
    base_version: str
    target_version: str
    added: list[dict[str, Any]] = field(default_factory=list)
    removed: list[dict[str, Any]] = field(default_factory=list)
    changed: list[dict[str, Any]] = field(default_factory=list)
    id_drift: list[dict[str, Any]] = field(default_factory=list)
    full_rewrite: bool = False
    summary: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Record:
    """遗留通用记录流水线的数据载体（向后兼容）。"""
    key: str
    data: dict[str, Any] = field(default_factory=dict)


class PullError(Exception):
    """下载阶段确定性错误（缺分片/损坏/超限/陈旧响应）。"""


class ValidationError(Exception):
    """校验阻断异常，携带报告。"""

    def __init__(self, report: ValidationReport):
        self.report = report
        super().__init__(f"validation blocked: "
                         f"{sum(1 for f in report.findings if f.severity == 'BLOCKER')} blocker(s)")


class VersionConflict(Exception):
    """同日多版本激活冲突等换版规则冲突。"""
