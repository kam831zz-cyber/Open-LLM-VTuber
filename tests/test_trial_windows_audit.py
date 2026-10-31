"""Exact Windows Popen audit matching, no real process/model/API calls."""
import ast
import json
from pathlib import Path
import runpy
import subprocess
import sys
import tempfile
import time
import types
import unittest
from unittest.mock import MagicMock,patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'tools/readiness_trial'))
from monitor import Journal
from windows_backend import WindowsBackend


class WindowsTrialAuditTests(unittest.TestCase):
    def guard(self,runtime,child=False):
        source=ROOT/'tools/readiness_trial/guard.py'
        module=runpy.run_path(str(source),run_name='guard_offline_test')
        with patch('sys.addaudithook') as hook:
            module['install'](runtime,port=None if child else 12394,ollama=not child)
        return hook.call_args.args[0]

    def argv(self,runtime):
        return [sys.executable,'-I','-B',str((ROOT/'tools/readiness_trial/guard.py').resolve()),
                '--child',str(Path(runtime).resolve()),'script',str((ROOT/'scripts/weather_mcp_server.py').resolve())]

    def test_windows_serialized_command_is_accepted_without_parsing(self):
        with tempfile.TemporaryDirectory() as runtime:
            guard=self.guard(runtime);argv=self.argv(runtime)
            guard('subprocess.Popen',(None,subprocess.list2cmdline(argv),runtime,{}))

    def test_exact_list_and_tuple_commands_remain_compatible(self):
        with tempfile.TemporaryDirectory() as runtime:
            guard=self.guard(runtime);argv=self.argv(runtime)
            for command in (argv,tuple(argv)):
                guard('subprocess.Popen',(sys.executable,command,runtime,{}))

    def test_extra_command_arguments_and_unapproved_entrypoints_are_rejected(self):
        with tempfile.TemporaryDirectory() as runtime:
            guard=self.guard(runtime);argv=self.argv(runtime)
            for command in (subprocess.list2cmdline(argv)+' & extra',argv+['--extra'],argv[:-1]+['unknown.py']):
                with self.subTest(command_type=type(command).__name__),self.assertRaises(PermissionError):
                    guard('subprocess.Popen',(None,command,runtime,{}))

    def test_wrong_executable_or_working_directory_is_rejected(self):
        with tempfile.TemporaryDirectory() as runtime:
            guard=self.guard(runtime);command=subprocess.list2cmdline(self.argv(runtime))
            for executable,cwd in (('cmd.exe',runtime),(None,str(Path(runtime).parent))):
                with self.subTest(cwd_matches=cwd==runtime),self.assertRaises(PermissionError):
                    guard('subprocess.Popen',(executable,command,cwd,{}))

    def test_child_cannot_start_even_an_approved_parent_command(self):
        with tempfile.TemporaryDirectory() as runtime:
            guard=self.guard(runtime,child=True)
            with self.assertRaises(PermissionError):
                guard('subprocess.Popen',(None,subprocess.list2cmdline(self.argv(runtime)),runtime,{}))

    def function(self,name,env):
        tree=ast.parse((ROOT/'tools/readiness_trial/candidate.py').read_text())
        node=next(n for n in ast.walk(tree) if isinstance(n,ast.FunctionDef) and n.name==name)
        exec(compile(ast.Module(body=[node],type_ignores=[]),'trial-function','exec'),env)
        return env[name]

    def test_asr_timings_emitted_before_later_context_initialization(self):
        events=[]
        def initialize(engine):engine.initialization_metrics={'model_construct_ms':20,'native_import_ms':10}
        function=self.function('measured_asr_init',{'asr_init':initialize,'emit':lambda *a,**kw:events.append((a,kw))})
        function(types.SimpleNamespace())
        self.assertEqual(events,[(('asr_model_construct_complete',),{'model_construct_ms':20,'native_import_ms':10})])

    def test_stage_emission_includes_common_clock(self):
        with tempfile.TemporaryDirectory() as directory:
            fake_time=types.SimpleNamespace(monotonic=lambda:4,monotonic_ns=lambda:4_000_000_000)
            emit=self.function('emit',{'time':fake_time,'started':1,'runtime':Path(directory),'json':json})
            emit('fixture')
            event=json.loads((Path(directory)/'stages.jsonl').read_text())
            self.assertEqual((event['elapsed_s'],event['monotonic_ns']),(3,4_000_000_000))

    def test_low_memory_stop_does_not_wait_for_cooperative_grace(self):
        with tempfile.TemporaryDirectory() as directory:
            journal=Journal(directory);journal.data['stop_reason']='available_RAM_below_1.5GiB'
            backend=WindowsBackend(directory,directory,journal=journal,api=MagicMock())
            backend.process=MagicMock();backend.process.poll.return_value=None;backend.assigned=True
            result=backend.request_stop()
            backend.process.wait.assert_not_called()
            self.assertEqual(result['cooperative_grace_seconds'],0)
            self.assertTrue((Path(directory)/'stop').exists())

    def test_normal_cleanup_preserves_three_second_grace(self):
        with tempfile.TemporaryDirectory() as directory:
            journal=Journal(directory);journal.data['stop_reason']='websocket_observed'
            backend=WindowsBackend(directory,directory,journal=journal,api=MagicMock())
            backend.process=MagicMock();backend.process.poll.return_value=None;backend.assigned=True
            backend.request_stop()
            backend.process.wait.assert_called_once_with(timeout=3)
