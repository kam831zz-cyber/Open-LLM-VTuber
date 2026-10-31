"""One inert worker: no app, model, network, child process or microphone imports."""
import argparse
import json
from pathlib import Path
import os
import time

p=argparse.ArgumentParser()
p.add_argument('--runtime',required=True)
a=p.parse_args()
runtime=Path(a.runtime).resolve()
# Assignment to the private Job must precede any worker action.
while not (runtime/'owned.start').exists():time.sleep(.02)
(runtime/'dummy-started.json').write_text(json.dumps({'pid':os.getpid(),'kind':'model_free_dummy'}),encoding='utf-8')
# Deliberately stay alive on the cooperative stop marker: verify Job close itself.
while True:time.sleep(.1)
