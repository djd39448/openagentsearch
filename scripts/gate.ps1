param([string]$Contribution, [string]$Results = 'gate-results.jsonl', [double]$Timeout = 30)
$GateFailed = 0
C:\Users\trustcore-rdp\tools\uv-0.12.3\uv.exe run ruff check .
if ((-not $?) -or ($LASTEXITCODE -ne 0)) { $GateFailed = 1 }
C:\Users\trustcore-rdp\tools\uv-0.12.3\uv.exe run ruff format --check .
if ((-not $?) -or ($LASTEXITCODE -ne 0)) { $GateFailed = 1 }
C:\Users\trustcore-rdp\tools\uv-0.12.3\uv.exe run mypy src
if ((-not $?) -or ($LASTEXITCODE -ne 0)) { $GateFailed = 1 }
C:\Users\trustcore-rdp\tools\uv-0.12.3\uv.exe run pytest -q
if ((-not $?) -or ($LASTEXITCODE -ne 0)) { $GateFailed = 1 }
if ($Contribution) { $root = Split-Path $PSScriptRoot -Parent; $env:PYTHONPATH = "$root\src;$root"; & 'C:\Users\trustcore-rdp\AppData\Roaming\uv\python\cpython-3.12-windows-x86_64-none\python.exe' -m scripts.gate --contribution $Contribution --results $Results --timeout $Timeout; if ($LASTEXITCODE -ne 0) { $GateFailed = 1 } }
if ($GateFailed -ne 0) { exit 1 }
exit 0
