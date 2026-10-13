"""Device-local run mode. Voice preferences remain in their original settings file."""
import hashlib
import json
import os
import platform
from pathlib import Path
from uuid import uuid4


class DeviceProfileStore:
    def __init__(self, directory=None):
        device=hashlib.sha256(platform.node().casefold().encode()).hexdigest()[:16]
        base=Path(os.environ.get('LOCALAPPDATA',str(Path.home())))/'Ativia'/'devices'/device
        self.directory=Path(directory or os.environ.get('ATIVIA_DEVICE_DIR') or base)
        self.path=self.directory/'runtime.json'

    def load(self):
        try:
            value=json.loads(self.path.read_text(encoding='utf-8')).get('mode')
            return value if value in ('text','voice') else 'voice'
        except (OSError,ValueError,AttributeError): return 'voice'

    def save(self,mode):
        if mode not in ('text','voice'):raise ValueError('Unknown device mode')
        self.directory.mkdir(parents=True,exist_ok=True)
        temp=self.path.with_name(uuid4().hex+'.tmp')
        try:
            temp.write_text(json.dumps({'version':1,'mode':mode}),encoding='utf-8')
            temp.replace(self.path)
        finally:temp.unlink(missing_ok=True)
