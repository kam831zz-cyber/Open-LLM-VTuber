"""Mock-only checks of exited-process races and trial restrictions."""
import ast
import ctypes
from ctypes import wintypes
from pathlib import Path
import runpy
import types
import unittest
from unittest.mock import MagicMock, patch

ROOT=Path(__file__).resolve().parents[1]


class TrialCleanupTests(unittest.TestCase):
    def identity_function(self, exited=False, race_exit=False):
        source=ast.parse((ROOT/'tools/readiness_trial/windows_owner.py').read_text())
        node=next(n for n in source.body if isinstance(n,ast.FunctionDef) and n.name=='identity')
        kernel=MagicMock()
        kernel.OpenProcess.return_value=123
        def times(handle,created,end,cpu,user):
            ctypes.cast(created,ctypes.POINTER(ctypes.c_ulonglong))[0]=7
            ctypes.cast(end,ctypes.POINTER(ctypes.c_ulonglong))[0]=99 if exited else 0
            return True
        kernel.GetProcessTimes.side_effect=times
        kernel.QueryFullProcessImageNameW.return_value=False
        def exitcode(handle,value):
            ctypes.cast(value,ctypes.POINTER(wintypes.DWORD))[0]=0 if race_exit else 259
            return True
        kernel.GetExitCodeProcess.side_effect=exitcode
        class Memory(ctypes.Structure):
            _fields_=[('cb',wintypes.DWORD),('ws',ctypes.c_size_t),('private',ctypes.c_size_t)]
        env={'k':kernel,'c':ctypes,'w':wintypes,'ProcessMemory':Memory,'psapi':MagicMock()}
        exec(compile(ast.Module(body=[node],type_ignores=[]),'identity','exec'),env)
        return env['identity'],kernel

    def test_already_exited_does_not_query_image_path(self):
        identity,kernel=self.identity_function(exited=True)
        self.assertTrue(identity(42)['exited_or_inaccessible'])
        kernel.QueryFullProcessImageNameW.assert_not_called()
        kernel.CloseHandle.assert_called_once_with(123)

    def test_exit_between_time_and_image_queries_is_not_live(self):
        identity,kernel=self.identity_function(race_exit=True)
        self.assertTrue(identity(42)['exited_or_inaccessible'])
        kernel.CloseHandle.assert_called_once()

    def test_live_image_failure_remains_an_explicit_error(self):
        identity,kernel=self.identity_function()
        with self.assertRaises(OSError):identity(42)
        kernel.CloseHandle.assert_called_once()

    def guard(self,**kwargs):
        module=runpy.run_path(str(ROOT/'tools/readiness_trial/guard.py'),run_name='trial_guard_test')
        with patch('sys.addaudithook') as register:
            module['install'](ROOT/'runtime-fixture',**kwargs)
        return register.call_args.args[0]

    def test_child_guard_blocks_network_and_process_creation(self):
        guard=self.guard()
        for event,args in [('socket.connect',(None,('203.0.113.1',443))),
                           ('subprocess.Popen',('python',['python','bad.py'])),
                           ('open',(str(ROOT/'outside.txt'),'w',0))]:
            with self.subTest(event=event),self.assertRaises(PermissionError):guard(event,args)

    def test_candidate_network_scope_is_only_ollama_loopback(self):
        guard=self.guard(port=12394,ollama=True)
        guard('socket.connect',(None,('127.0.0.1',11434)))
        for port in (18081,18000,50021):
            with self.subTest(port=port),self.assertRaises(PermissionError):
                guard('socket.connect',(None,('127.0.0.1',port)))
