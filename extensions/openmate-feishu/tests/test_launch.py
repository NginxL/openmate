"""Starting under LarkAgentX's Python: reading initialize without taking more, the decision, the hand-over of the
line through a pipe, and the real launcher re-executing itself."""

import _runtmp  # noqa: F401  (first: every temporary directory of this run in one, removed at its end)
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
EXT_DIR = os.path.dirname(HERE)
ROOT = os.path.dirname(os.path.dirname(EXT_DIR))
for p in (os.path.join(ROOT, "core"), EXT_DIR):
    if p not in sys.path:
        sys.path.insert(0, p)

from openmate_feishu import launch  # noqa: E402

LAUNCHER = os.path.join(EXT_DIR, "bin", "openmate-feishu")


def init_line(config, **extra) -> bytes:
    params = {"protocol": 1, "hub": {"token": "pext_secret_token"}, "extension": {"id": "openmate-feishu"}, "config": config}
    params.update(extra)
    return (json.dumps({"id": "h1", "method": "initialize", "params": params}) + "\n").encode("utf-8")


def piped(data: bytes) -> int:
    """A file descriptor that reads data: a pipe, or an unlinked file for more than a pipe holds."""
    if len(data) <= 8192:
        r, w = os.pipe()
        os.write(w, data)
        os.close(w)
        return r
    with tempfile.TemporaryFile() as f:
        f.write(data)
        f.flush()
        fd = os.dup(f.fileno())
    os.lseek(fd, 0, os.SEEK_SET)
    return fd


class Done(Exception):
    pass


class ReadTest(unittest.TestCase):
    def test_one_line_and_not_a_byte_more(self):
        fd = piped(b'{"id":"h1"}\n{"id":"h2"}\n')
        self.addCleanup(os.close, fd)
        self.assertEqual(launch.read_line(fd), b'{"id":"h1"}\n')
        self.assertEqual(os.read(fd, 100), b'{"id":"h2"}\n', "the rest stays for the core")
        fd2 = piped(b"abcdef")
        self.addCleanup(os.close, fd2)
        self.assertEqual(launch.read_line(fd2, most=4), b"abcd")

    def test_initialize_config(self):
        self.assertEqual(launch.initialize_config(init_line({"larkx_python": "/x"})), {"larkx_python": "/x"})
        self.assertEqual(launch.initialize_config(init_line(None)), {})
        self.assertIsNone(launch.initialize_config(b'{"id":"h1","method":"manifest"}\n'))
        self.assertIsNone(launch.initialize_config(b"not json\n"))
        self.assertIsNone(launch.initialize_config(b"\xff\n"))


class CheckTest(unittest.TestCase):
    def test_paths(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        plain = os.path.join(d, "python")
        with open(plain, "w") as f:
            f.write("")
        os.chmod(plain, 0o644)
        self.assertIn("绝对路径", launch.check_python("venv/bin/python"))
        self.assertIn("不存在", launch.check_python(os.path.join(d, "missing")))
        self.assertIn("执行权限", launch.check_python(plain))
        self.assertEqual(launch.check_python(sys.executable), "", "this interpreter is fine")

    def test_versions(self):
        exe = sys.executable

        def run_with(out, code=0, raise_=None):
            def run(argv, **kw):
                self.assertEqual(argv[:3], [exe, "-I", "-S"])
                if raise_:
                    raise raise_
                return subprocess.CompletedProcess(argv, code, out, b"")
            return run

        self.assertIn("3.8", launch.check_python(exe, run_with(b"3.8")))
        self.assertEqual(launch.check_python(exe, run_with(b"3.12")), "")
        self.assertEqual(launch.check_python(exe, run_with(b"3.9")), "")
        self.assertIn("运行不了", launch.check_python(exe, run_with(b"", 1)))
        self.assertIn("运行不了", launch.check_python(exe, run_with(b"-I -S -c")))
        self.assertIn("运行不了", launch.check_python(exe, run_with(b"", raise_=subprocess.TimeoutExpired("x", 1))))

    def test_decide(self):
        ok = lambda p: ""  # noqa: E731
        self.assertEqual(launch.decide({}, {"larkx_python": "/venv/bin/python"}, ok), ("exec", "/venv/bin/python"))
        self.assertEqual(launch.decide({"OPENMATE_BACKEND": "larkagentx"}, {"larkx_python": " /p "}, ok), ("exec", "/p"))
        self.assertEqual(launch.decide({"OPENMATE_BACKEND": "fixture"}, {"larkx_python": "/p"}, ok), ("stay", ""))
        self.assertEqual(launch.decide({}, {}, ok), ("stay", ""), "unset: the backend says not_configured")
        self.assertEqual(launch.decide({}, None, ok), ("stay", ""), "not initialize")
        self.assertEqual(launch.decide({}, {"larkx_python": "p"}, lambda p: "bad"), ("stay", "bad"))


class HandoffTest(unittest.TestCase):
    def setUp(self):
        self.addCleanup(setattr, launch, "_problem", launch._problem)
        launch._problem = ""

    def exec_into(self, line, env=None):
        calls = []

        def execve(path, argv, child_env):
            fd = int(child_env[launch.FIRST_FD])
            calls.append((path, argv, child_env, launch.read_all(fd)))
            os.close(fd)
            raise Done()

        fd = piped(line + b'{"id":"h2","method":"ping"}\n')
        self.addCleanup(os.close, fd)
        with self.assertRaises(Done):
            launch.handoff(LAUNCHER, env=dict(env or {}), stdin_fd=fd, execve=execve, check=lambda p: "")
        self.assertEqual(os.read(fd, 1000), b'{"id":"h2","method":"ping"}\n', "the rest is the new program's")
        return calls[0]

    def test_exec_hands_the_line_over_a_pipe(self):
        line = init_line({"larkx_python": "/venv/bin/python"})
        path, argv, env, got = self.exec_into(line, {"PATH": "/usr/bin"})
        self.assertEqual(path, "/venv/bin/python")
        self.assertEqual(argv, ["/venv/bin/python", "-I", "-B", LAUNCHER])
        self.assertEqual(env[launch.CHILD], "/venv/bin/python")
        self.assertEqual(env["PATH"], "/usr/bin")
        self.assertEqual(got, line)
        for v in list(env.values()) + argv:
            self.assertNotIn("pext_secret_token", v, "the token never goes through the environment or the command line")

    def test_a_long_line_is_written_by_a_helper(self):
        import warnings

        line = init_line({"larkx_python": "/venv/bin/python", "triage_rules": "规" * 40000})
        self.assertGreater(len(line), 65536 * 1.5)
        with warnings.catch_warnings():
            # the launcher forks before any thread exists; this test process has threads of other tests
            warnings.simplefilter("ignore", DeprecationWarning)
            self.assertEqual(self.exec_into(line)[3], line)

    def test_the_re_executed_program_reads_the_pipe(self):
        fd = piped(b"first line\n")
        env = {launch.CHILD: "/venv/bin/python", launch.FIRST_FD: str(fd)}
        self.assertEqual(launch.handoff(LAUNCHER, env=env, stdin_fd=-1), b"first line\n")
        self.assertNotIn(launch.FIRST_FD, env)
        with self.assertRaises(OSError):
            os.fstat(fd)  # closed
        self.assertEqual(launch.handoff(LAUNCHER, env={launch.CHILD: "/p"}, stdin_fd=-1), b"")

    def test_requests_before_initialize_are_answered_as_the_core_does(self):
        from openmate_core.app import ConnectorApp
        import openmate_feishu

        early = ConnectorApp(openmate_feishu.platform(), env={}).handle
        line = init_line({"larkx_python": "/venv/bin/python"})
        wrong = json.dumps({"id": "h3", "method": "initialize", "params": {"protocol": 101}}) + "\n"
        before = ('{"id":"h0","method":"ping"}\n{"method":"note"}\n{"id":"h2","method":"manifest"}\n' + wrong).encode()
        stdin = piped(before + line + b'{"id":"h9","method":"ping"}\n')
        self.addCleanup(os.close, stdin)
        out_r, out_w = os.pipe()
        self.addCleanup(os.close, out_r)
        calls = []

        def execve(path, argv, env):
            fd = int(env[launch.FIRST_FD])
            calls.append(launch.read_all(fd))
            os.close(fd)
            raise Done()

        with self.assertRaises(Done):
            launch.handoff(LAUNCHER, env={}, stdin_fd=stdin, stdout_fd=out_w, execve=execve, check=lambda p: "", early=early)
        os.close(out_w)
        self.assertEqual(calls, [line], "the choice is still made on initialize")
        answers = [json.loads(x) for x in launch.read_all(out_r).splitlines()]
        self.assertEqual([a["id"] for a in answers], ["h0", "h2", "h3"])
        self.assertEqual(answers[0]["error"]["code"], "not_initialized")
        self.assertEqual(answers[1]["result"]["id"], "openmate-feishu")
        self.assertNotIn("tags", answers[1]["result"])
        self.assertEqual(answers[2]["error"]["code"], "wrong_protocol")
        self.assertEqual(os.read(stdin, 100), b'{"id":"h9","method":"ping"}\n')

    def test_the_input_ends_before_initialize(self):
        stdin = piped(b'{"id":"h0","method":"ping"}\n')
        self.addCleanup(os.close, stdin)
        out_r, out_w = os.pipe()
        self.addCleanup(os.close, out_r)
        self.addCleanup(os.close, out_w)
        self.assertEqual(launch.handoff(LAUNCHER, env={}, stdin_fd=stdin, stdout_fd=out_w, execve=self.fail,
                                        early=lambda m, p: {}), b"")

    def test_stays_with_the_reason(self):
        fd = piped(init_line({"larkx_python": "relative/python"}))
        self.addCleanup(os.close, fd)
        line = launch.handoff(LAUNCHER, env={}, stdin_fd=fd, execve=self.fail)
        self.assertTrue(line.startswith(b'{"id": "h1"'))
        self.assertIn("绝对路径", launch.problem())

    def test_fixture_reads_nothing(self):
        fd = piped(b"untouched\n")
        self.addCleanup(os.close, fd)
        self.assertEqual(launch.handoff(LAUNCHER, env={"OPENMATE_BACKEND": "fixture"}, stdin_fd=fd, execve=self.fail), b"")
        self.assertEqual(os.read(fd, 100), b"untouched\n")

    def test_exec_failure_is_the_status(self):
        def execve(path, argv, env):
            raise PermissionError("denied")

        fd = piped(init_line({"larkx_python": "/venv/bin/python"}))
        self.addCleanup(os.close, fd)
        line = launch.handoff(LAUNCHER, env={}, stdin_fd=fd, execve=execve, check=lambda p: "")
        self.assertTrue(line)
        self.assertIn("没能用 LarkAgentX 的 Python 启动", launch.problem())


class LauncherTest(unittest.TestCase):
    """The real launcher, as the hub starts it: initialize names an interpreter, and the program becomes it."""

    def spawn(self, config):
        if os.path.join(ROOT, "tests") not in sys.path:
            sys.path.insert(0, os.path.join(ROOT, "tests"))
        from harness import StdioHub

        data = tempfile.mkdtemp(prefix="openmate-launch-")
        self.addCleanup(shutil.rmtree, data, True)
        env = {k: v for k, v in os.environ.items() if not k.startswith(("OPENMATE_", "PERSONA_", "PYTHON"))}
        env["PERSONA_EXTENSION_DATA"] = data
        p = subprocess.Popen([sys.executable, LAUNCHER], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, env=env)
        self.addCleanup(self._stop, p)
        hub = StdioHub(p.stdout, p.stdin)
        from harness import HubSideError

        with self.assertRaises(HubSideError) as e:  # as the hub's conformance check does first
            hub.request("ping", {})
        self.assertEqual(e.exception.code, "not_initialized")
        res = hub.request("initialize", json.loads(init_line(config))["params"])
        self.assertEqual(res["id"], "openmate-feishu")
        return p, hub

    @staticmethod
    def _stop(p):
        if p.poll() is None:
            p.kill()
            p.wait(5)
        for f in (p.stdin, p.stdout, p.stderr):
            f.close()

    def status(self, hub):
        deadline = time.monotonic() + 15
        while True:
            res = hub.request("connector.status", {"contribution": "matefeishu"})
            if res["state"] != "error" or time.monotonic() > deadline:
                return res
            time.sleep(0.05)

    def command_line(self, pid):
        try:
            return subprocess.run(["ps", "-o", "command=", "-p", str(pid)], stdout=subprocess.PIPE,
                                  stderr=subprocess.DEVNULL, timeout=10).stdout.decode("utf-8", "replace").strip()
        except (OSError, subprocess.SubprocessError):
            return ""

    def test_re_executes_under_the_configured_python(self):
        p, hub = self.spawn({"larkx_python": sys.executable})
        st = self.status(hub)
        # this interpreter has no LarkAgentX: the re-executed program says so (it got initialize through the pipe)
        self.assertEqual(st["state"], "not_configured")
        self.assertIn("没有装 LarkAgentX", st["error"])
        cmd = self.command_line(p.pid)
        if cmd:  # ps may be unavailable in a sandbox
            self.assertIn(" -I -B " + LAUNCHER, cmd)
        p.stdin.close()
        self.assertEqual(p.wait(10), 0)
        self.assertNotIn(b"pext_secret_token", p.stderr.read())

    def test_an_unusable_python_is_the_status(self):
        p, hub = self.spawn({"larkx_python": "venv/bin/python"})
        st = self.status(hub)
        self.assertEqual(st["state"], "not_configured")
        self.assertIn("绝对路径", st["error"])
        cmd = self.command_line(p.pid)
        if cmd:
            self.assertNotIn(" -I -B ", cmd, "not re-executed")

    def test_unset_is_not_configured(self):
        st = self.status(self.spawn({})[1])
        self.assertEqual(st["state"], "not_configured")
        self.assertIn("larkx_python", st["error"])


if __name__ == "__main__":
    unittest.main()
