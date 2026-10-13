"""The Cube adapter's log lines carry no row values: the real server.js against stand-ins for express and Cube.

server.js prints into a server.log next to the results, and agents are pointed at results. PostgreSQL and Cube error
messages quote the values they choke on (an id compared with a number column is echoed back) in a quoting style that
depends on the server's language, so the adapter logs a query error only as its class, code and a coarse category,
keeps model compile errors (they quote only the generated model), hides free-form console output of the Cube
packages, and prints the full text only when SXC_CUBE_DEBUG=1. The stand-in modules make server.js call its
logger with errors shaped like Cube's (a stack string, a pg error object, a message quoting SQL) and print console
chatter; no database, Cube install or network is needed, only node on PATH.
"""

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import pgfixture  # noqa: F401  (puts the scripts directory on sys.path)
from semantic_crosscheck import cube_run, procutil

NODE = procutil.which("node")
SERVER_JS = cube_run.ADAPTER_DIR / "server.js"
VALUES = ("P-000123", "P-000456", "P-000789", "4242", "FIRM", "SELECT")

EXPRESS = """
function express() {
  return {
    use() {},
    listen(port, host, cb) {
      setImmediate(cb);
      return { address: () => ({ port: 4321, host }) };
    },
  };
}
express.json = () => (req, res, next) => next();
module.exports = express;
"""

CORE = """
class CubejsServerCore {
  constructor(options) { this.options = options; }
  initApp() {
    const mode = process.env.FAKE_MODE || '';
    if (mode === 'throw') throw new Error('cannot load value "P-000123" at 4242');
    if (mode === 'reject') return Promise.reject(new Error("startup failed for 'P-000456'"));
    const log = this.options.logger;
    log('Error while querying', { error: 'error: invalid input syntax for type integer: "P-000123"\\n    at Parser (x.js:1:2)',
                                  query: 'SELECT 1 FROM part WHERE part_id = $1', values: ['P-000123'] });
    const pg = new Error('invalid input syntax for type integer: "P-000456"');
    pg.name = 'error';
    pg.code = '22P02';
    log('Orchestrator error', { error: pg });
    log('Dropping Cache', { error: "Error: SELECT * FROM t WHERE k = 'P-000789'" });
    log('Error querying db', { error: 'Error: ung\\u00fcltige Eingabesyntax f\\u00fcr Typ integer: \\u00bbP-000123\\u00ab' });
    log('Error querying db', { error: 'Error: syntaxe en entr\\u00e9e invalide pour le type integer : \\u00ab P-000456 \\u00bb' });
    log('Error querying db', { error: 'Error: canceling statement due to statement timeout' });
    log('Compiling schema', { error: 'Error: Compile errors:\\nm_x cube: unknown member part.nope\\n    at Compiler (C:\\\\Users\\\\someone\\\\x.js:1:2)' });
    log('Error querying db', { error: 'Error: invalid input syntax for type numeric: \\u00bbP-000789 compile errors: zq\\u00ab' });
    log('Performing query', { query: 'SELECT 1', values: ['P-000789'] });
    console.log('pool says', 'FIRM', 4242);
    console.error('again FIRM');
    return Promise.resolve();
  }
}
module.exports = { CubejsServerCore };
"""


@unittest.skipUnless(NODE, "node is not on PATH")
class AdapterLogTest(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="sxc_t_adapter_"))
        self.addCleanup(shutil.rmtree, self.dir, True)
        shutil.copyfile(SERVER_JS, self.dir / "server.js")
        for rel, text in (("express/index.js", EXPRESS), ("@cubejs-backend/server-core/index.js", CORE)):
            path = self.dir / "node_modules" / rel
            path.parent.mkdir(parents=True)
            path.write_text(text, encoding="utf-8")

    def run_adapter(self, **extra) -> tuple[int, list[str]]:
        env = {k: v for k, v in os.environ.items() if not k.upper().startswith(("SXC_", "CUBEJS_", "NODE_OPTIONS"))}
        env.update({"CUBEJS_API_SECRET": "s" * 64, "SXC_PORT": "0", **extra})
        p = subprocess.run([NODE, "server.js"], cwd=str(self.dir), env=env, input="", capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=120)
        return p.returncode, (p.stdout + p.stderr).splitlines()

    def assert_no_values(self, lines: list[str]):
        for value in VALUES:
            with self.subTest(value=value):
                self.assertFalse([ln for ln in lines if value in ln], f"{value} reached the log")

    def test_errors_are_logged_as_class_code_and_category_only(self):
        """No message text of a query error survives, whatever quoting the server's language uses (German and French
        guillemets included); model compile errors are kept because they quote only the generated model."""
        code, lines = self.run_adapter()
        self.assertEqual(code, 0, lines)
        self.assert_no_values(lines)
        self.assertIn("SXC_READY 4321", lines)
        self.assertIn("SXC_CUBE_ERROR Error while querying: Error", lines)
        self.assertIn("SXC_CUBE_ERROR Orchestrator error: Error [22P02]", lines)
        self.assertIn("SXC_CUBE_ERROR Dropping Cache: Error", lines)
        self.assertEqual(lines.count("SXC_CUBE_ERROR Error querying db: Error"), 3,
                         "a value containing 'compile errors' does not make a query error verbatim")
        self.assertIn("SXC_CUBE_ERROR Error querying db: Error (timeout)", lines)
        self.assertIn("SXC_CUBE_ERROR Compiling schema: Error (model): Error: Compile errors: m_x cube: unknown member part.nope",
                      lines)
        self.assertFalse([ln for ln in lines if "»" in ln or "«" in ln or "integer" in ln or "numeric" in ln],
                         "message text dropped")
        self.assertFalse([ln for ln in lines if "someone" in ln or "x.js" in ln], "stack frames of model errors dropped")
        self.assertEqual(sum(ln.startswith("SXC_CUBE_ERROR") for ln in lines), 8, "events without an error are dropped")
        self.assertEqual([ln for ln in lines if ln.startswith("SXC_CONSOLE")],
                         ["SXC_CONSOLE console output of the Cube packages is hidden (SXC_CUBE_DEBUG=1 shows it)"])
        self.assertFalse([ln for ln in lines if "Parser" in ln or "x.js" in ln], "stack frames are dropped")
        self.assertFalse([ln for ln in lines if ln.startswith("SXC_DEBUG")])

    def test_debug_mode_adds_the_full_text(self):
        code, lines = self.run_adapter(SXC_CUBE_DEBUG="1")
        self.assertEqual(code, 0, lines)
        debug = [ln for ln in lines if ln.startswith("SXC_DEBUG")]
        self.assertEqual(len(debug), 8)
        self.assertIn('"P-000123"', debug[0])
        self.assertIn("pool says FIRM 4242", lines)
        self.assertIn("SXC_CUBE_ERROR Error while querying: Error", lines,
                      "the redacted line stays, so the runner's startup message never quotes values")

    def test_startup_failures_are_redacted_too(self):
        for mode, prefix in (("throw", "SXC_ERROR uncaught exception: Error"),
                             ("reject", "SXC_ERROR startup failed: Error")):
            with self.subTest(mode=mode):
                code, lines = self.run_adapter(FAKE_MODE=mode)
                self.assertEqual(code, 1, lines)
                self.assert_no_values(lines)
                self.assertIn(prefix, lines)
                self.assertNotIn("SXC_READY 4321", lines)

    def test_missing_secret_stops_before_serving(self):
        code, lines = self.run_adapter(CUBEJS_API_SECRET="short")
        self.assertEqual(code, 2)
        self.assertIn("SXC_ERROR CUBEJS_API_SECRET must be set by the runner", lines)


if __name__ == "__main__":
    unittest.main()
