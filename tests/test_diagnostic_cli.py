"""Opt-in diagnostics remain standalone, fail closed and do not leak caller values."""
import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import pgfixture
import sxc
from semantic_crosscheck.result import Result, StageError, EXIT_CONFIG


class DiagnosticCliTests(unittest.TestCase):
    def parse(self, argv):
        with contextlib.redirect_stderr(io.StringIO()):
            try:
                return sxc.parser().parse_args(argv)
            except SystemExit:
                self.fail('diagnostic CLI rejected supported options')

    def test_repair_entry_point_is_standalone(self):
        args = self.parse(['shacl-repair', '--spec', 'fixture.json'])
        self.assertEqual(args.spec, 'fixture.json')
        self.assertEqual(args.timeout_ms, 30000)
        self.assertEqual(sxc.config_path('shacl-repair', None), None)
        with self.assertRaises(StageError):
            sxc.config_path('shacl-repair', 'crosscheck.toml')

    def test_grain_requires_configuration(self):
        args = self.parse(['grain', '--spec', 'grain.json', '--max-rows', '20'])
        self.assertEqual(args.max_rows, 20)
        self.assertIn('grain', sxc.NEEDS_CONFIG)

    def test_reason_explanation_defaults_are_opt_in_and_bounded(self):
        args = self.parse(['reason'])
        self.assertFalse(args.explain_inconsistency)
        self.assertEqual((args.explain_max_axioms,args.explain_max_checks,args.explain_timeout_ms),(128,64,10000))
        args = self.parse(['reason','--explain-inconsistency','--explain-max-axioms','8','--explain-max-checks','9','--explain-timeout-ms','100'])
        self.assertTrue(args.explain_inconsistency)
        self.assertEqual((args.explain_max_axioms,args.explain_max_checks,args.explain_timeout_ms),(8,9,100))

    def test_new_summaries_do_not_render_output_paths(self):
        for stage in ('shacl_repair','grain'):
            result=Result(stage);result.add('check:0001','not_run','',reason='unsupported')
            text=sxc._summary(result,Path('private-path-canary')/'result.json',False)
            self.assertNotIn('private-path-canary',text)

    def test_new_parser_errors_do_not_echo_private_values(self):
        for stage in ('grain','shacl-repair'):
            with self.subTest(stage=stage),tempfile.TemporaryDirectory() as d:
                spec=Path(d)/'input.json';spec.write_text('{}',encoding='utf-8')
                stdout,stderr=io.StringIO(),io.StringIO()
                with contextlib.redirect_stdout(stdout),contextlib.redirect_stderr(stderr):
                    code=sxc.main([stage,'--spec',str(spec),'--unknown','private-canary-073','--out',str(Path(d)/'out')])
                self.assertEqual(code,2)
                self.assertNotIn('private-canary-073',stdout.getvalue()+stderr.getvalue())
                self.assertNotIn(str(spec),stdout.getvalue()+stderr.getvalue())

    def test_malformed_manifest_errors_do_not_echo_spec_path_or_contents(self):
        for stage in ('grain','shacl-repair'):
            with self.subTest(stage=stage),tempfile.TemporaryDirectory() as d:
                spec=Path(d)/'private-canary-file.json';spec.write_text('{"secret":"private-canary-074"',encoding='utf-8')
                argv=[stage,'--spec',str(spec),'--out',str(Path(d)/'out')]
                if stage=='grain':argv.extend(['--config',str(pgfixture.DEMO_CONFIG)])
                stdout,stderr=io.StringIO(),io.StringIO()
                with contextlib.redirect_stdout(stdout),contextlib.redirect_stderr(stderr):code=sxc.main(argv)
                self.assertEqual(code,2)
                self.assertNotIn('private-canary',stdout.getvalue()+stderr.getvalue())

    def test_grain_protects_gold_and_revision_before_global_cleanup(self):
        from types import SimpleNamespace
        for kind in ('gold','revision'):
            for filename in ('result.json','result.json.part'):
                with self.subTest(kind=kind,filename=filename),tempfile.TemporaryDirectory() as d:
                    root=Path(d);out=root/'out';target=out/'grain'/filename
                    target.parent.mkdir(parents=True);target.write_text('SELECT 71 AS protected',encoding='utf-8')
                    cfgpath=root/'config.toml';cfgpath.write_text('version = 1',encoding='utf-8')
                    spec=root/'spec.json';spec.write_text('{}',encoding='utf-8')
                    fake=SimpleNamespace(path=cfgpath,base_dir=lambda:root,
                        gold=[SimpleNamespace(sql_path=lambda cfg:target)] if kind=='gold' else [],
                        source=SimpleNamespace(data_revision_sql_file=str(target) if kind=='revision' else ''))
                    args=SimpleNamespace(stage='grain',spec=str(spec),config=str(cfgpath),out=str(out),quiet=False)
                    with patch('sxc.config.load',return_value=fake),self.assertRaises(StageError):
                        sxc._diagnostic_prepare(args,out)
                    self.assertEqual(target.read_text(encoding='utf-8'),'SELECT 71 AS protected')
                    stdout,stderr=io.StringIO(),io.StringIO()
                    with patch('sxc.config.load',return_value=fake),contextlib.redirect_stdout(stdout),contextlib.redirect_stderr(stderr):
                        self.assertEqual(sxc._diagnostic_usage_failure(args),2)
                    self.assertEqual(target.read_text(encoding='utf-8'),'SELECT 71 AS protected')

    def test_output_directory_blockers_are_preserved_without_traceback(self):
        for stage in ('grain','shacl-repair'):
            with self.subTest(stage=stage),tempfile.TemporaryDirectory() as d:
                root=Path(d);out=root/'private-canary-out';out.mkdir()
                blocked=out/stage.replace('-','_');blocked.write_text('do not destroy',encoding='utf-8')
                spec=root/'spec.json';spec.write_text('{}',encoding='utf-8')
                argv=[stage,'--spec',str(spec),'--out',str(out)]
                if stage=='grain':argv.extend(['--config',str(pgfixture.DEMO_CONFIG)])
                stdout,stderr=io.StringIO(),io.StringIO()
                with contextlib.redirect_stdout(stdout),contextlib.redirect_stderr(stderr):code=sxc.main(argv)
                self.assertNotEqual(code,0)
                self.assertNotIn('private-canary',stdout.getvalue()+stderr.getvalue())
                self.assertEqual(blocked.read_text(encoding='utf-8'),'do not destroy')

    def test_repair_forbidden_config_alias_is_preserved(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);out=root/'out';target=out/'shacl_repair'/'result.json';target.parent.mkdir(parents=True)
            target.write_text('protected config input',encoding='utf-8')
            spec=root/'spec.json';spec.write_text('{}',encoding='utf-8')
            with contextlib.redirect_stdout(io.StringIO()),contextlib.redirect_stderr(io.StringIO()):
                code=sxc.main(['shacl-repair','--config',str(target),'--spec',str(spec),'--out',str(out)])
            self.assertEqual(code,2);self.assertEqual(target.read_text(encoding='utf-8'),'protected config input')

    def test_successful_diagnostic_write_failure_becomes_sanitized_error(self):
        with tempfile.TemporaryDirectory() as d:
            result=Result('grain');result.add('c0001','pass')
            with patch.object(Result,'write',side_effect=PermissionError('private-canary-075')):
                path=sxc._diagnostic_write(result,Path(d))
            self.assertIsNone(path);self.assertEqual(result.status,'error')
            self.assertNotIn('private-canary',json.dumps(result.to_dict()))

    def test_usage_failure_does_not_follow_unrelated_part_hardlinks(self):
        import os
        for stage in ('grain','shacl-repair'):
            with self.subTest(stage=stage),tempfile.TemporaryDirectory() as d:
                root=Path(d);out=root/'out';part=out/stage.replace('-','_')/'result.json.part';part.parent.mkdir(parents=True)
                sentinel=root/'unrelated.txt';sentinel.write_text('do not overwrite',encoding='utf-8');os.link(sentinel,part)
                spec=pgfixture.ROOT/'examples'/('grain/contract.json' if stage=='grain' else 'shacl-repair/valid.json')
                argv=[stage,'--spec',str(spec),'--out',str(out),'--unknown']
                if stage=='grain':argv.extend(['--config',str(pgfixture.ROOT/'examples/grain/crosscheck.toml')])
                with contextlib.redirect_stdout(io.StringIO()),contextlib.redirect_stderr(io.StringIO()):code=sxc.main(argv)
                self.assertEqual(code,2)
                self.assertEqual(sentinel.read_text(encoding='utf-8'),'do not overwrite')

    def test_grain_configuration_drift_at_dispatch_or_return_never_passes(self):
        from types import SimpleNamespace
        from semantic_crosscheck import grain
        original=grain.run
        for phase in ('entry','return'):
            with self.subTest(phase=phase),tempfile.TemporaryDirectory() as d:
                root=Path(d);cfgpath=root/'crosscheck.toml'
                config_text=(pgfixture.ROOT/'examples/grain/crosscheck.toml').read_text(encoding='utf-8')
                cfgpath.write_text(config_text,encoding='utf-8')
                spec=pgfixture.ROOT/'examples/grain/contract.json'
                evidence={key:0 for key in grain._KEY_VIOLATIONS}
                evidence.update(complete=True,source_rows=1,node_rows=1,selected_rows=1,excluded_null_rows=0)
                def changed(cfg,args,out):
                    if phase=='entry':cfgpath.write_text(config_text.replace('agg = "sum"','agg = "count"'),encoding='utf-8')
                    result=original(cfg,args,out)
                    if phase=='return':cfgpath.write_text(config_text.replace('agg = "sum"','agg = "count"'),encoding='utf-8')
                    return result
                with patch.object(grain,'run',side_effect=changed),patch.object(grain.db,'connect',return_value=SimpleNamespace(close=lambda:None)),patch.object(grain.db,'read_only',return_value=contextlib.nullcontext()),patch.object(grain,'evaluate',return_value=evidence),contextlib.redirect_stdout(io.StringIO()),contextlib.redirect_stderr(io.StringIO()):
                    code=sxc.main(['grain','--config',str(cfgpath),'--spec',str(spec),'--out',str(root/'out')])
                self.assertNotEqual(code,0)
                saved=json.loads((root/'out/grain/result.json').read_text(encoding='utf-8'))
                self.assertNotEqual(saved['status'],'pass')


if __name__=='__main__':unittest.main()
