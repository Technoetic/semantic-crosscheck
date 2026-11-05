"""Simulate Darwin root aliases without weakening declared-path link guards."""
from pathlib import PurePosixPath
import stat
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import pgfixture  # noqa: F401 - package path setup only
from semantic_crosscheck import boundary_schema as boundary
from semantic_crosscheck.result import StageError


class SimulatedPath(PurePosixPath):
    """Only metadata/path operations; no real filesystem mutation on any OS."""
    metadata = {}
    targets = {}
    canonical = {}

    def absolute(self):
        if not self.is_absolute():
            raise AssertionError('Simulation requires absolute selected paths')
        return self

    def lstat(self):
        try:
            return self.metadata[str(self)]
        except KeyError:
            raise FileNotFoundError from None

    def readlink(self):
        try:
            return SimulatedPath(self.targets[str(self)])
        except KeyError:
            raise OSError('Not a simulated link') from None

    def resolve(self, strict=False):
        return SimulatedPath(self.canonical.get(str(self), str(self)))


def metadata(kind, reparse=False):
    return SimpleNamespace(st_mode=kind | 0o755, st_file_attributes=0x400 if reparse else 0)


class BoundaryPlatformPathsTest(unittest.TestCase):
    def setUp(self):
        SimulatedPath.metadata = {'/': metadata(stat.S_IFDIR), '/private': metadata(stat.S_IFDIR)}
        SimulatedPath.targets = {}
        SimulatedPath.canonical = {}

    def alias(self, name='var', *, target=None, canonical=None):
        alias = '/' + name
        expected = '/private/' + name
        SimulatedPath.metadata[alias] = metadata(stat.S_IFLNK)
        SimulatedPath.metadata[expected] = metadata(stat.S_IFDIR)
        SimulatedPath.targets[alias] = target if target is not None else 'private/' + name
        SimulatedPath.canonical[alias] = canonical if canonical is not None else expected
        for suffix in ('folders', 'folders/sxc', 'folders/sxc/out'):
            SimulatedPath.metadata[alias + '/' + suffix] = metadata(stat.S_IFDIR)
        SimulatedPath.metadata[alias + '/folders/sxc/spec.json'] = metadata(stat.S_IFREG)
        return alias

    def check(self, path, *, platform='darwin', directory=True):
        with patch.object(boundary, 'Path', SimulatedPath), \
                patch.object(boundary, 'sys', SimpleNamespace(platform=platform), create=True):
            return boundary._unlinked(SimulatedPath(path), directory=directory)

    def test_exact_darwin_standard_root_aliases_accept_normal_input_and_output_paths(self):
        for name in ('var', 'tmp'):
            for raw_target in ('private/' + name, '/private/' + name):
                with self.subTest(alias=name, raw_target=raw_target):
                    alias = self.alias(name, target=raw_target)
                    try:
                        self.check(alias + '/folders/sxc/out')
                        self.check(alias + '/folders/sxc/spec.json', directory=False)
                    except StageError:
                        self.fail('Normal Darwin OS root alias must not reject a plain selected path')

    def test_standard_alias_allowance_is_darwin_only(self):
        alias = self.alias()
        for platform in ('linux', 'win32'):
            with self.subTest(platform=platform), self.assertRaises(StageError):
                self.check(alias + '/folders/sxc/out', platform=platform)

    def test_wrong_readlink_targets_cannot_use_the_standard_root_alias_exception(self):
        for target in ('/outside/var', 'outside/var', '/private/tmp', 'private/x/../var'):
            with self.subTest(target=target):
                alias = self.alias(target=target)
                with self.assertRaises(StageError):
                    self.check(alias + '/folders/sxc/out')

    def test_redirected_canonical_target_or_linked_target_is_rejected(self):
        alias = self.alias(canonical='/outside/var')
        with self.assertRaises(StageError):
            self.check(alias + '/folders/sxc/out')
        alias = self.alias()
        for target_metadata in (metadata(stat.S_IFLNK), metadata(stat.S_IFREG), metadata(stat.S_IFDIR, reparse=True)):
            with self.subTest(mode=target_metadata.st_mode, attributes=target_metadata.st_file_attributes):
                SimulatedPath.metadata['/private/var'] = target_metadata
                with self.assertRaises(StageError):
                    self.check(alias + '/folders/sxc/out')

    def test_user_input_output_and_nested_directory_symlinks_remain_rejected(self):
        alias = self.alias()
        selected = [('/var/folders/sxc/source-link.json', False),
                    ('/var/folders/sxc/output-link', True),
                    ('/var/folders/sxc/user-link/child', True)]
        for path, directory in selected:
            with self.subTest(path=path):
                link = '/var/folders/sxc/user-link' if path.endswith('/child') else path
                SimulatedPath.metadata[link] = metadata(stat.S_IFLNK)
                SimulatedPath.targets[link] = '/elsewhere'
                # A blanket resolve of the selected path would erase this link.
                SimulatedPath.canonical[path] = '/plain-resolved/child'
                with self.assertRaises(StageError):
                    self.check(path, directory=directory)

    def test_reparse_points_and_unrelated_root_aliases_are_never_admitted(self):
        self.alias()
        SimulatedPath.metadata['/var'] = metadata(stat.S_IFDIR, reparse=True)
        with self.assertRaises(StageError):
            self.check('/var/folders/sxc/out')
        self.alias('etc')
        with self.assertRaises(StageError):
            self.check('/etc/folders/sxc/out')

    def test_os_root_alias_itself_is_not_a_regular_input_file(self):
        alias = self.alias()
        with self.assertRaises(StageError):
            self.check(alias, directory=False)


if __name__ == '__main__':
    unittest.main()
