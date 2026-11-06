"""Build the separately licensed native-UIA reader with the existing toolchain."""
import hashlib
import json
import os
from pathlib import Path
import subprocess


def build(root=None):
    root = root or Path(__file__).resolve().parent
    compiler = Path(os.environ.get('WINDIR', r'C:\Windows')) / 'Microsoft.NET/Framework64/v4.0.30319/csc.exe'
    if not compiler.is_file():
        raise RuntimeError('.NET Framework C# compiler is missing')
    source = root / 'cursor_reader/OpenLessCursorReader.cs'
    target = source.with_suffix('.exe')
    subprocess.run([str(compiler), '/nologo', '/target:exe', '/platform:x64', '/optimize+',
                    '/r:System.Web.Extensions.dll', '/r:System.Windows.Forms.dll', '/r:System.Drawing.dll',
                    '/out:' + str(target), str(source)],
                   check=True, creationflags=subprocess.CREATE_NO_WINDOW)
    return {'status': 'built', 'reader': str(target), 'source_sha256': hashlib.sha256(source.read_bytes()).hexdigest(),
            'binary_sha256': hashlib.sha256(target.read_bytes()).hexdigest()}


if __name__ == '__main__':
    print(json.dumps(build()))
