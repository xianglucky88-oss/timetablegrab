"""命令行入口（离线，默认走 fixtures 本地文件源）。

子命令：
  run             遗留通用记录流水线
  ingest SOURCE   时刻表整包摄取（下载→解析→校验→对齐→差分→登记/换版）
  versions        列出来源版本状态
  activate TOKEN  手动激活到日版本
  withdraw TOKEN  撤回版本
  query TABLE     按当前生效版本查询（不跨版本拼接）
  recover         中断后状态收敛
"""
import json
import sys

from . import config, feed_pull, pipeline, store, version


def _print(obj):
    print(json.dumps(obj, ensure_ascii=False, indent=2, default=str))


def _file_roots():
    # 离线约定：var/sources/<source>/manifest.json
    base = config.ROOT / "var" / "sources"
    return {p.name: p for p in base.glob("*") if p.is_dir()}


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    cmd = argv[0] if argv else "run"

    if cmd == "run":
        _print(pipeline.run())
        return 0

    con = store.connect()
    try:
        if cmd == "ingest":
            if len(argv) < 2:
                print("usage: ingest SOURCE", file=sys.stderr)
                return 2
            source = argv[1]
            transport = feed_pull.FileTransport(_file_roots())
            _print(pipeline.ingest_feed(source, transport, con=con))
            return 0
        if cmd == "versions":
            source = argv[1] if len(argv) > 1 else None
            if source:
                _print(store.list_versions(con, source))
            else:
                _print({s: store.list_versions(con, s)
                        for s in {r[0] for r in con.execute(
                            "SELECT DISTINCT feed_source FROM feed_versions")}})
            return 0
        if cmd == "activate":
            source, token = argv[1], argv[2]
            _print({"state": version.activate(con, source, token)})
            return 0
        if cmd == "withdraw":
            source, token = argv[1], argv[2]
            _print({"state": version.withdraw(con, source, token,
                                              reason=" ".join(argv[3:]))})
            return 0
        if cmd == "query":
            if len(argv) < 2:
                print("usage: query TABLE [--token T | --date D]", file=sys.stderr)
                return 2
            table = argv[1]
            token = argv[argv.index("--token") + 1] if "--token" in argv else None
            on_date = argv[argv.index("--date") + 1] if "--date" in argv else None
            _print(version.query_table(con, _default_source(con), table,
                                       token=token, on_date=on_date))
            return 0
        if cmd == "recover":
            source = argv[1]
            _print(version.recover(con, source))
            return 0
        print(f"unknown command: {cmd}", file=sys.stderr)
        return 2
    finally:
        con.close()


def _default_source(con):
    row = con.execute("SELECT feed_source FROM feed_versions LIMIT 1").fetchone()
    return row[0] if row else ""


if __name__ == "__main__":
    sys.exit(main())
