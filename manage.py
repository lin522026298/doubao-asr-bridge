"""Local service control; only manages this installation's bridge process."""
import pathlib
import subprocess
import sys
import urllib.request
import psutil

ROOT = pathlib.Path(__file__).resolve().parent
PID = ROOT/'data/service.pid'

def alive():
    try:
        p=psutil.Process(int(PID.read_text()))
        return p if any(str(ROOT/'bridge.py').lower()==a.lower() for a in p.cmdline()) else None
    except (OSError,ValueError,psutil.Error):
        return None

if __name__ == '__main__':
    action=sys.argv[1] if len(sys.argv)>1 else 'start'
    p=alive()
    if action=='stop':
        if p:
            # Windows venv pythonw can be a redirector with the actual interpreter
            # as its child. Stop both, otherwise the service port remains owned.
            children = p.children(recursive=True)
            for child in children:
                try: child.terminate()
                except psutil.NoSuchProcess: pass
            try: p.terminate()
            except psutil.NoSuchProcess: pass
            try: p.wait(timeout=5)
            except psutil.TimeoutExpired: p.kill(); p.wait(timeout=3)
            _, remaining = psutil.wait_procs(children, timeout=3)
            for child in remaining:
                try: child.kill()
                except psutil.NoSuchProcess: pass
    elif action=='login':
        subprocess.Popen([sys.executable,str(ROOT/'login.py')],cwd=ROOT)
    elif not p:
        PID.parent.mkdir(exist_ok=True)
        with (PID.parent/'service-errors.log').open('ab') as log:
            p=subprocess.Popen([sys.executable,str(ROOT/'bridge.py')],cwd=ROOT,
                stdout=log,stderr=log,creationflags=subprocess.CREATE_NO_WINDOW)
        PID.write_text(str(p.pid))
