"""Apply the shared simulated Darwin path cases to plan-compare guards."""
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import test_boundary_platform_paths as platform_paths
from test_plan_compare import pc
from semantic_crosscheck.result import StageError


class PlanComparePlatformPathsTest(platform_paths.BoundaryPlatformPathsTest):
    def check(self, path, *, platform='darwin', directory=True):
        # The shared fixture models only relevant metadata. Regular files used
        # by plan-compare also need the single-link count required by its guard.
        for meta in platform_paths.SimulatedPath.metadata.values():
            meta.st_nlink = 1
        with patch.object(pc, 'Path', platform_paths.SimulatedPath), \
                patch.object(pc, 'sys', SimpleNamespace(platform=platform), create=True):
            try:
                return pc._inspect(platform_paths.SimulatedPath(path), directory=directory)
            except (OSError, ValueError):
                raise StageError('invalid_or_protected_inputs', 2) from None

    def test_system_alias_does_not_hide_canonical_input_output_collision(self):
        self.alias()
        source = '/var/folders/sxc/spec.json'
        target = '/private/var/folders/sxc/spec.json'
        with patch.object(pc, 'Path', platform_paths.SimulatedPath):
            platform_paths.SimulatedPath.canonical[source] = target
            self.assertTrue(pc._aliases(platform_paths.SimulatedPath(source), platform_paths.SimulatedPath(target)))


if __name__ == '__main__':
    unittest.main()
