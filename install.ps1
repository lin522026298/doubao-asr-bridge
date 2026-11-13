$ErrorActionPreference = 'Stop'
$target = Join-Path $env:LOCALAPPDATA 'Programs\DoubaoAsrBridge'
$openless = Join-Path $env:LOCALAPPDATA 'Programs\OpenLess\openless.exe'
if (!(Test-Path -LiteralPath $openless)) { throw 'Install OpenLess in its default per-user location first.' }
if (Test-Path -LiteralPath $target) { throw 'Installation already exists. Refusing to overwrite an existing installation or login.' }
$compiler = Join-Path $env:WINDIR 'Microsoft.NET\Framework64\v4.0.30319\csc.exe'
if (!(Test-Path -LiteralPath $compiler)) { throw '.NET Framework C# compiler is missing.' }
& py -3.11 -c 'import sys; assert sys.version_info[:2] == (3, 11)'
if ($LASTEXITCODE -ne 0) { throw 'Python 3.11 is required.' }
New-Item -ItemType Directory -Path $target | Out-Null
$files = @('bridge.py','batch_asr.py','asr_errors.py','credential_store.py','login_state.py','login_notice.py','login.py','manage.py','configure_openless.py',
    'openai_credentials.py','openai_realtime.py','realtime_profile.py','realtime_text.py','realtime-prompt.txt','configure_realtime.py','bridge_version.py','cursor_context.py','build_cursor_reader.py',
    'OpenLessLauncher.cs','requirements.txt','README.md','LICENSE','LICENSE-Murmur.txt')
foreach ($file in $files) { Copy-Item -LiteralPath (Join-Path $PSScriptRoot $file) -Destination $target }
$reader = Join-Path $target 'cursor_reader'
New-Item -ItemType Directory -Path $reader | Out-Null
foreach ($file in @('OpenLessCursorReader.cs','UPSTREAM.md','LICENSE-AGPL-3.0.txt')) {
    Copy-Item -LiteralPath (Join-Path $PSScriptRoot ('cursor_reader\' + $file)) -Destination $reader
}
$docs = Join-Path $target 'docs'
New-Item -ItemType Directory -Path $docs | Out-Null
foreach ($file in @('realtime.md','batch-api.md')) {
    Copy-Item -LiteralPath (Join-Path $PSScriptRoot ('docs\' + $file)) -Destination $docs
}
& py -3.11 -m venv (Join-Path $target 'runtime')
if ($LASTEXITCODE -ne 0) { throw 'Virtual environment creation failed.' }
$python = Join-Path $target 'runtime\Scripts\python.exe'
& $python -m pip install -r (Join-Path $target 'requirements.txt')
if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed.' }
& $python (Join-Path $target 'build_cursor_reader.py')
if ($LASTEXITCODE -ne 0) { throw 'Cursor reader compilation failed.' }
$launcher = Join-Path $target 'OpenLessLauncher.exe'
& $compiler /nologo /target:winexe "/out:$launcher" (Join-Path $target 'OpenLessLauncher.cs')
if ($LASTEXITCODE -ne 0) { throw 'Launcher compilation failed.' }
$shell = New-Object -ComObject WScript.Shell
$link = $shell.CreateShortcut((Join-Path ([Environment]::GetFolderPath('Desktop')) 'OpenLess（豆包联动）.lnk'))
$link.TargetPath = $launcher
$link.WorkingDirectory = $target
$link.IconLocation = "$openless,0"
$link.Save()
$login = $shell.CreateShortcut((Join-Path $target '登录豆包.lnk'))
$login.TargetPath = Join-Path $target 'runtime\Scripts\pythonw.exe'
$login.Arguments = '"' + (Join-Path $target 'login.py') + '"'
$login.WorkingDirectory = $target
$login.Save()
$cursor = $shell.CreateShortcut((Join-Path $target 'Realtime光标上下文设置.lnk'))
$cursor.TargetPath = Join-Path $target 'runtime\Scripts\pythonw.exe'
$cursor.Arguments = '"' + (Join-Path $target 'configure_realtime.py') + '" cursor --cursor-mode gui'
$cursor.WorkingDirectory = $target
$cursor.Save()
Write-Host 'Installed. Follow README: login, exit OpenLess, configure channels, launch from the new shortcut.'
