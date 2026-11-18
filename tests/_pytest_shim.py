"""极简 pytest 兼容层：仅用于本机无 pytest 环境时跑通测试，不参与交付代码。

支持本项目实际用到的能力：fixture（含 tmp_path/monkeypatch）、raises、
mark（占位）、funcarg 注入、简单断言。测试目录在 sys.path 上时优先于真 pytest。
"""
import contextlib
import importlib
import inspect
import pathlib
import re
import sys
import tempfile
import traceback
import types

_pytest = types.ModuleType("pytest")


class _Raises:
    def __init__(self, exc, match=None):
        self.exc, self.match, self.value = exc, match, None

    def __enter__(self):
        return self

    def __exit__(self, et, ev, tb):
        if et is None:
            raise AssertionError(f"expected {self.exc.__name__} but nothing raised")
        if not issubclass(et, self.exc):
            return False
        self.value = ev
        if self.match and not re.search(self.match, str(ev)):
            raise AssertionError(f"pattern {self.match!r} not in {ev!s}")
        return True


def raises(exc, match=None):
    return _Raises(exc, match)


class MonkeyPatch:
    def __init__(self):
        self._undo = []

    def setattr(self, obj, name, value):
        old = getattr(obj, name)
        self._undo.append((obj, name, old))
        setattr(obj, name, value)

    def undo(self):
        for obj, name, old in reversed(self._undo):
            setattr(obj, name, old)
        self._undo.clear()


class _Mark:
    def __getattr__(self, _):
        def deco(fn):
            return fn
        return deco


_pytest.raises = raises
_pytest.mark = _Mark()
_pytest.fixture = lambda fn=None, **kw: (fn if callable(fn) else (lambda f: f))

sys.modules.setdefault("pytest", _pytest)


class _Fail(Exception):
    pass


def _collect(mod):
    funcs = [getattr(mod, n) for n in dir(mod) if n.startswith("test_")]
    return [f for f in funcs if callable(f)]


def _build_fixtures(mod):
    fixtures = {}
    for name in dir(mod):
        obj = getattr(mod, name)
        if callable(obj) and name in ("sandbox", "con"):
            fixtures[name] = obj
    return fixtures


def _resolve(name, fixtures, cache, tmp, mp):
    if name in cache:
        return cache[name]
    if name == "tmp_path":
        cache[name] = tmp
    elif name == "monkeypatch":
        cache[name] = mp
    elif name in fixtures:
        deps = inspect.signature(fixtures[name]).parameters
        kwargs = {d: _resolve(d, fixtures, cache, tmp, mp) for d in deps}
        result = fixtures[name](**kwargs)
        if inspect.isgenerator(result):
            cache[name] = next(result)
        else:
            cache[name] = result
    else:
        raise KeyError(f"unknown fixture {name!r}")
    return cache[name]


def main():
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))
    import pytest  # noqa: F401  (register shim)

    test_files = sorted(p for p in
                        pathlib.Path(__file__).resolve().parent.glob("test_*.py"))
    passed = failed = 0
    failures = []
    for tf in test_files:
        mod = importlib.import_module(tf.stem)
        conftest = importlib.import_module("conftest")
        fx = _build_fixtures(conftest)
        for fn in _collect(mod):
            tmp = pathlib.Path(tempfile.mkdtemp(prefix=f"{tf.stem}_"))
            mp = MonkeyPatch()
            cache: dict = {}
            names = inspect.signature(fn).parameters
            try:
                values = {n: _resolve(n, fx, cache, tmp, mp) for n in names}
                fn(**values)
                passed += 1
                print(f"PASS {tf.stem}::{fn.__name__}")
            except Exception:
                failed += 1
                failures.append((tf.stem, fn.__name__, traceback.format_exc()))
                print(f"FAIL {tf.stem}::{fn.__name__}")
            finally:
                if "con" in cache:
                    cache["con"].close()
                mp.undo()
    print(f"\n{passed} passed, {failed} failed")
    for mod_name, fn_name, tb in failures:
        print(f"\n=== {mod_name}::{fn_name} ===\n{tb}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
