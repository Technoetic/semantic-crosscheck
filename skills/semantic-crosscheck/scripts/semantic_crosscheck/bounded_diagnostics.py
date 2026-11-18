"""Guarded CLI boundary for the two optional bounded diagnostics.

Neither diagnostic participates in default operational report authority. Raw
arguments and configuration errors are never rendered. File observations detect
ordinary changes; they are not a lock against hostile filesystem races.
"""
import argparse
from dataclasses import dataclass
import importlib
import json
import os
from pathlib import Path
import sys

from . import config
from .result import EXIT_CONFIG, EXIT_FAIL, Result, StageError, discard_own_verdict, inputs_fingerprint

STAGES = {'measure-oracle': 'measure_oracle', 'plan-compare': 'plan_compare'}
FILE_LIMIT = 4 * 1024 * 1024


class _Invalid(Exception):
    pass


class _Parser(argparse.ArgumentParser):
    def error(self, _message):
        raise _Invalid()


def _raw_options(argv, name):
    values = []
    tokens = list(argv)
    if '--' in tokens:
        tokens = tokens[:tokens.index('--')]
    for index, token in enumerate(tokens):
        option, separator, value = token.partition('=')
        if option != name:
            continue
        if separator:
            values.append(value)
        elif index + 1 < len(tokens) and not tokens[index + 1].startswith('--'):
            values.append(tokens[index + 1])
        else:
            values.append(None)
    return values


def _arguments(argv, stage):
    if not argv or argv[0] != stage:
        raise _Invalid()
    parser = _Parser(prog='sxc.py ' + stage, allow_abbrev=False,
                     description='Optional bounded diagnostic; a pass does not establish global or business correctness.')
    parser.add_argument('--spec', action='append', required=True, metavar='FILE',
                        help=('version 1 or 2 independent raw rule and Cube query' if stage == 'measure-oracle'
                              else 'strict version 1 bounded diagnostic specification'))
    parser.add_argument('--config', action='append', metavar='FILE',
                        help='operational crosscheck.toml (default: ./crosscheck.toml)' if stage == 'measure-oracle' else argparse.SUPPRESS)
    parser.add_argument('--out', action='append', metavar='DIR',
                        help='local output directory (default: .sxc next to config)' if stage == 'measure-oracle' else
                        'local output directory (default: ./.sxc)')
    parser.add_argument('--quiet', action='count', default=0, help='print only the summary line')
    if stage == 'plan-compare':
        parser.add_argument('--dsn-env', action='append', metavar='NAME',
                            help='environment variable naming the dedicated synthetic connection (default: SXC_TEST_ADMIN_DSN)')
        parser.add_argument('--statement-timeout-ms', action='append', type=int, metavar='N',
                            help='per-statement time limit (default: 1000)')
    args = parser.parse_args(argv[1:])
    args.stage = stage
    setattr(args, '_' + STAGES[stage] + '_inputs',
            [value for option in ('--spec', '--config') for value in _raw_options(argv, option) if value])
    for name in ('spec', 'config', 'out', 'dsn_env', 'statement_timeout_ms'):
        values = getattr(args, name, None)
        if values is not None:
            if len(values) != 1 or (isinstance(values[0], str) and (not values[0] or '\0' in values[0])):
                raise _Invalid()
            setattr(args, name, values[0])
    if args.quiet > 1:
        raise _Invalid()
    if stage == 'plan-compare':
        if args.config is not None:
            raise _Invalid()
        args.dsn_env = args.dsn_env or 'SXC_TEST_ADMIN_DSN'
        args.statement_timeout_ms = 1000 if args.statement_timeout_ms is None else args.statement_timeout_ms
    return args


def _candidate_out(argv, stage):
    values = _raw_options(argv, '--out')
    if values:
        return Path(values[0]) if len(values) == 1 and values[0] and '\0' not in values[0] else None
    if stage == 'measure-oracle':
        configs = _raw_options(argv, '--config')
        if configs:
            return Path(configs[0]).parent / '.sxc' if len(configs) == 1 and configs[0] and '\0' not in configs[0] else None
    return Path.cwd() / '.sxc'


def _clear_prior(argv, stage, out=None, protected_paths=()):
    try:
        configs = [value for value in _raw_options(argv, '--config') if value]
        if not configs and stage == 'measure-oracle':
            configs = ['crosscheck.toml']
        protected = [*argv, *protected_paths]
        # Invalid/repeated flags and newly edited configurations still select
        # all declared files. Recheck their current scope even when captures
        # provided an earlier configuration's protected paths.
        module = importlib.import_module('semantic_crosscheck.' + STAGES[stage])
        candidate = out if out is not None else _candidate_out(argv, stage)
        if candidate is None:
            return
        directory = Path(candidate) / STAGES[stage]
        module._inspect(candidate, directory=True, missing=True)
        module._inspect(directory, directory=True, missing=True)
        for target in (Path(candidate) / '.gitignore', directory / 'result.json', directory / 'result.json.part'):
            module._inspect(target, missing=True)
        for value in configs:
            source = _Capture.read(value, module._inspect)
            protected.append(value)
            if source.raw is None:
                continue
            try:
                selected = config.loads(source.raw, value)
            except Exception:
                if b'result.json' in source.raw:
                    return
            else:
                protected.extend(_configuration_paths(selected))
        discard_own_verdict(candidate, STAGES[stage], protected_paths=protected)
    except Exception:
        pass


@dataclass
class _Capture:
    path: Path
    raw: bytes | None
    identity: tuple | None

    @classmethod
    def read(cls, path, inspect, *, required=False):
        path = Path(path)
        meta = inspect(path, missing=True)
        if meta is None:
            # Some module guards return no metadata; existence is checked only
            # after their link/reparse and ancestor checks have succeeded.
            try:
                meta = path.lstat()
            except FileNotFoundError:
                if required:
                    raise _Invalid() from None
                return cls(path, None, None)
        if meta.st_size > FILE_LIMIT:
            raise _Invalid()
        with path.open('rb') as stream:
            raw = stream.read(FILE_LIMIT + 1)
            current = os.fstat(stream.fileno())
        # Windows can expose different ctime semantics through fstat/lstat.
        # Compare the open handle's object, size and mtime, then compare both
        # path observations using the same lstat API (including ctime).
        if len(raw) > FILE_LIMIT or cls._identity(meta)[:4] != cls._identity(current)[:4]:
            raise _Invalid()
        inspect(path)
        if cls._identity(meta) != cls._identity(path.lstat()):
            raise _Invalid()
        return cls(path, raw, cls._identity(meta))

    @staticmethod
    def _identity(meta):
        return (meta.st_dev, meta.st_ino, meta.st_size, meta.st_mtime_ns, meta.st_ctime_ns)

    def unchanged(self, inspect):
        current = self.read(self.path, inspect)
        if current.raw != self.raw or current.identity != self.identity:
            raise _Invalid()


def _configuration_paths(cfg):
    paths = [cfg.path, *(gold.sql_path(cfg) for gold in cfg.gold)]
    if cfg.source.data_revision_sql_file:
        paths.append(cfg.base_dir() / cfg.source.data_revision_sql_file)
    if cfg.semantic_preflight is not None:
        paths.extend(cfg.semantic_preflight.paths(cfg))
    if cfg.data_rules is not None:
        paths.append(cfg.data_rules.path(cfg))
    for gold in cfg.gold:
        for case in gold.expect:
            if case.spec_source is not None:
                paths.append(cfg.base_dir() / case.spec_source.file)
    return paths


def _validate(module, args, out, captures, paths, result=None):
    module.prepare_run(args, out, protected_paths=paths)
    for source in captures:
        source.unchanged(module._inspect)
    callback = getattr(result, '_diagnostic_validate', None)
    if callback is not None and callback() is not True:
        raise _Invalid()


def _remove_published(result, out, module, args):
    """Delete only our exact, still-unlinked receipt; preserve another writer."""
    try:
        paths, configuration = [], None
        if args.config:
            configuration = _Capture.read(args.config, module._inspect)
            if configuration.raw is not None:
                try:
                    paths = _configuration_paths(config.loads(configuration.raw, args.config))
                except Exception:
                    if b'result.json' in configuration.raw:
                        return
        # A changed configuration may have selected this very receipt as an
        # input after publication. Source preservation still wins over cleanup.
        module.prepare_run(args, out, protected_paths=paths)
        published = _Capture.read(Path(out) / result.stage / 'result.json', module._inspect, required=True)
        expected = json.dumps(result.to_dict(), ensure_ascii=False, indent=1).encode('utf-8')
        if published.raw == expected:
            module.prepare_run(args, out, protected_paths=paths)
            if configuration is not None:
                configuration.unchanged(module._inspect)
            published.unchanged(module._inspect)
            published.path.unlink()
    except Exception:
        pass


def _publish(result, module, args, out, captures, paths, ensure_out):
    _validate(module, args, out, captures, paths, result)
    ensure_out(out)
    _validate(module, args, out, captures, paths, result)
    try:
        result.write(out)
        _validate(module, args, out, captures, paths, result)
    except Exception:
        _remove_published(result, out, module, args)
        raise


def _error(stage, reason):
    result = Result(STAGES[stage])
    result._suppress_paths = True
    result.add('diagnostic', 'error', reason=reason)
    return result


def cli(argv, summary, ensure_out):
    stage = next((token for token in argv if token in STAGES), None)
    try:
        args = _arguments(argv, stage)
    except SystemExit as error:
        return error.code
    except Exception:
        _clear_prior(argv, stage)
        sys.stderr.write('[sxc ' + STAGES[stage] + '] ERROR invalid_arguments\n')
        return EXIT_CONFIG
    module = importlib.import_module('semantic_crosscheck.' + STAGES[stage])
    if stage == 'measure-oracle' and args.config is None:
        args.config = 'crosscheck.toml'
    out = Path(args.out) if args.out else (Path(args.config).parent if args.config else Path.cwd()) / '.sxc'
    captures, paths = [], []
    ready = False
    result = None
    exit_code = None
    try:
        # This guard also sees every raw --spec/--config occurrence before any
        # configuration read, output creation, or old-receipt cleanup.
        module.prepare_run(args, out)
        captures.append(_Capture.read(args.spec, module._inspect, required=True))
        cfg = None
        if stage == 'measure-oracle':
            captured_config = _Capture.read(args.config, module._inspect, required=True)
            captures.append(captured_config)
            cfg = config.loads(captured_config.raw, args.config)
            paths = _configuration_paths(cfg)
            module.prepare_run(args, out, protected_paths=paths)
            captures.extend(_Capture.read(path, module._inspect) for path in paths[1:])
        common_inputs = inputs_fingerprint(cfg) if cfg is not None else {}
        _validate(module, args, out, captures, paths)
        ensure_out(out)
        _validate(module, args, out, captures, paths)
        _clear_prior(argv, stage, out, paths)
        _validate(module, args, out, captures, paths)
        ready = True
        try:
            result = module.run(cfg, args, out)
            if not isinstance(result, Result) or result.stage != STAGES[stage]:
                raise _Invalid()
        except StageError as error:
            result, exit_code = _error(stage, 'invalid_or_unavailable_inputs'), error.exit_code
        except Exception:
            result, exit_code = _error(stage, 'execution_failed'), EXIT_FAIL
        result.inputs = {**common_inputs, **result.inputs}
        result._suppress_paths = True
        result.provenance = {'schema_version': 1, 'binding': 'not_applicable'}
        result.runtime_evidence = {'schema_version': 1, 'binding': 'not_applicable'}
        _publish(result, module, args, out, captures, paths, ensure_out)
    except Exception:
        if not ready:
            _clear_prior(argv, stage, out, paths)
        sys.stderr.write('[sxc ' + STAGES[stage] + '] ERROR invalid_or_changed_inputs\n')
        return EXIT_CONFIG
    print(summary(result, None, bool(args.quiet)))
    return exit_code if exit_code is not None else (EXIT_CONFIG if result.status == 'error' else result.exit_code())
