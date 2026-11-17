"""Validated container parameters; callers must separately authorize assets and volumes."""
from dataclasses import dataclass, field
from pathlib import PurePosixPath
import re


@dataclass(frozen=True)
class RunnerRequest:
    image: str
    argv: list[str]
    snapshot_volume: str
    task_volume: str
    timeout_seconds: int = 60
    output_bytes: int = 20 * 1024 * 1024
    container_name: str | None = None
    daemon_id: str | None = None
    lifecycle: object = field(default=None,repr=False,compare=False)

    def __post_init__(self):
        if not isinstance(self.image, str) or not re.fullmatch(r'sha256:[0-9a-f]{64}', self.image):
            raise ValueError('immutable image digest required')
        if not isinstance(self.argv, list) or not self.argv or len(self.argv) > 128 or any(not isinstance(v, str) or not v or '\x00' in v or len(v) > 65536 for v in self.argv):
            raise ValueError('invalid argv')
        if PurePosixPath(self.argv[0]).name in {'sh', 'bash', 'zsh', 'dash', 'ash'}:
            raise ValueError('shell execution denied')
        if not isinstance(self.snapshot_volume, str) or not re.fullmatch(r'dod-snapshot-[0-9a-f]{32}', self.snapshot_volume):
            raise ValueError('invalid snapshot volume')
        if not isinstance(self.task_volume, str) or not re.fullmatch(r'dod-task-[0-9a-f]{32}', self.task_volume):
            raise ValueError('invalid task volume')
        if type(self.timeout_seconds) is not int or not 1 <= self.timeout_seconds <= 60:
            raise ValueError('invalid timeout')
        if type(self.output_bytes) is not int or not 1 <= self.output_bytes <= 20 * 1024 * 1024:
            raise ValueError('invalid output limit')
        if self.daemon_id is not None and not re.fullmatch(r'[a-zA-Z0-9_.:-]{1,128}',self.daemon_id):
            raise ValueError('invalid daemon identity')
        if self.container_name is not None and not re.fullmatch(r'dod-run-[0-9a-f]{32}',self.container_name):
            raise ValueError('invalid container name')
        object.__setattr__(self, 'argv', tuple(self.argv))


def build_container_args(request, name):
    if not re.fullmatch(r'dod-run-[0-9a-f]{32}', name):
        raise ValueError('invalid container name')
    return ['docker', 'run', '--name', name, '--network=none', '--read-only', '--cpus=2', '--memory=2g',
        '--pids-limit=128', '--cap-drop=ALL', '--security-opt=no-new-privileges', '--user=65534:65534',
        '--mount=type=volume,src=' + request.snapshot_volume + ',dst=/workspace,readonly',
        '--mount=type=volume,src=' + request.task_volume + ',dst=/task',
        '--workdir=/workspace', request.image, *request.argv]


_VERIFY_SCRIPT = """import hashlib,json,os,stat,sys
path,expected,root,encoded=sys.argv[1:5]
manifest=json.loads(encoded)
source=None
if manifest is not None:
    found={}; total=0
    for directory,dirs,files in os.walk(root,followlinks=False):
        for name in dirs:
            if not stat.S_ISDIR(os.lstat(os.path.join(directory,name)).st_mode):
                raise SystemExit("invalid snapshot directory")
        for name in files:
            filename=os.path.join(directory,name)
            key=os.path.relpath(filename,root).replace(os.sep,"/")
            if key not in manifest or len(found)>=10000: raise SystemExit("snapshot mismatch")
            fd=os.open(filename,os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(fd,"rb") as f:
                if not stat.S_ISREG(os.fstat(f.fileno()).st_mode): raise SystemExit("invalid snapshot file")
                data=f.read(20971521)
            total+=len(data)
            if total>20971520 or hashlib.sha256(data).hexdigest()!=manifest[key]: raise SystemExit("snapshot mismatch")
            found[key]=manifest[key]
            if os.path.abspath(filename)==os.path.abspath(path): source=data
    if found!=manifest or source is None: raise SystemExit("snapshot mismatch")
else:
    with open(path,"rb") as f: source=f.read(20971521)
if len(source)>20971520 or hashlib.sha256(source).hexdigest()!=expected:
    raise SystemExit("approved script digest mismatch")
sys.argv=[path]
exec(compile(source,path,"exec"),{"__name__":"__main__","__file__":path,"__package__":None})
"""

def verified_script_argv(interpreter, path, sha256, *, root='/', manifest=None):
    import json
    if interpreter not in ('python','python3') or not isinstance(path,str) or not path.startswith('/') or '\x00' in path or not re.fullmatch(r'[0-9a-f]{64}',sha256):
        raise ValueError('invalid approved script')
    if not isinstance(root,str) or not root.startswith('/') or '\x00' in root:
        raise ValueError('invalid snapshot root')
    encoded=json.dumps(manifest,sort_keys=True,separators=(',',':'))
    if len(encoded.encode())>65536: raise ValueError('snapshot manifest exceeds limit')
    return [interpreter,'-I','-c',_VERIFY_SCRIPT,path,sha256,root,encoded]
