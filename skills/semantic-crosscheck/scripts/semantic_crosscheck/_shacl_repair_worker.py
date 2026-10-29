"""Private bounded repair-fixture worker. stdout carries fixed check JSON only."""
import contextlib
import json
from pathlib import Path
import sys


class _Discard:
    def write(self, value):
        return len(value)

    def flush(self):
        pass


def main():
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
        from semantic_crosscheck.shacl_repair import _execute
        source = Path(sys.argv[1])
        with contextlib.redirect_stdout(_Discard()), contextlib.redirect_stderr(_Discard()):
            checks = _execute(json.loads(source.read_text(encoding='utf-8')), source.parent)
        sys.stdout.write(json.dumps(checks, separators=(',', ':')))
        return 0
    except Exception:
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
