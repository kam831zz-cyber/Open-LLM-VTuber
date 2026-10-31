"""Mock-only regression runner: blocks model imports, processes and connections."""
import importlib.abc
import json
from pathlib import Path
import sys
import types
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
sys.path.insert(0,str(ROOT/'src'))


def audit(event,args):
    if event in {'subprocess.Popen','os.system'}:
        raise RuntimeError('Offline tests prohibit process creation')
    if event=='socket.connect':
        frame=sys._getframe()
        while frame:
            if frame.f_code.co_name=='socketpair' and Path(frame.f_code.co_filename).name=='socket.py':return
            frame=frame.f_back
        raise RuntimeError('Offline tests prohibit connections')


class BlockNative(importlib.abc.MetaPathFinder):
    def find_spec(self,fullname,path=None,target=None):
        if fullname.split('.')[0] in {'torch','onnxruntime','ctranslate2','av'}:
            raise ImportError('Native/model import forbidden by offline tests')


sys.addaudithook(audit)
sys.meta_path.insert(0,BlockNative())
numpy=types.ModuleType('numpy')
numpy.ndarray=list
numpy.float32=object()
numpy.zeros=lambda count,dtype=None:[0]*count
sys.modules['numpy']=numpy
whisper=types.ModuleType('faster_whisper')
def forbidden_model(*args,**kwargs):raise AssertionError('Real model construction forbidden')
whisper.WhisperModel=forbidden_model
sys.modules['faster_whisper']=whisper
from loguru import logger
logger.remove()

if __name__=='__main__':
    suite=unittest.defaultTestLoader.discover(str(ROOT/'tests'),pattern='test_*.py')
    result=unittest.TextTestRunner(verbosity=2).run(suite)
    report={'run':result.testsRun,'failures':len(result.failures),'errors':len(result.errors),
            'skipped':len(result.skipped),'real_model_loads':0,'real_mcp_processes':0,
            'native_imports_blocked':True,'network_connections_blocked':True}
    if len(sys.argv)>1:
        Path(sys.argv[1]).write_text(json.dumps(report,indent=2)+'\n',encoding='utf-8')
    sys.exit(not result.wasSuccessful())
