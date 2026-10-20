# Builds the Windows setup .exe (GitHub Actions runs this for every version):
#   powershell -ExecutionPolicy Bypass -File windows\build-release.ps1
# Needs Python 3.12 (the bundled one is the same version, for the compiled packages)
# and Inno Setup 6. Output: dist\StreamFinder-Setup-<version>.exe
param(
  [string]$Version,
  [string]$Commit
)
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'  # Invoke-WebRequest is very slow with the progress bar

$PythonVersion = '3.12.10'
$TorrServerTag = 'MatriX.147'
$Repo = Split-Path -Parent $PSScriptRoot
$Out = Join-Path $Repo 'build\StreamFinder'
$Downloads = Join-Path $Repo 'build\downloads'

function Assert-Ok($what) { if ($LASTEXITCODE -ne 0) { throw "$what failed (exit code $LASTEXITCODE)" } }

function Get-Download($url, $name) {
  $path = Join-Path $Downloads $name
  if (-not (Test-Path $path)) {
    Write-Host "==> Downloading $url"
    Invoke-WebRequest -Uri $url -OutFile "$path.part" -UseBasicParsing
    Move-Item "$path.part" $path
  }
  return $path
}

if (-not $Version) {
  $parts = (python (Join-Path $Repo 'streamfinder\version.py')).Split(' ')
  $Version = $parts[0]
  if (-not $Commit -and $parts.Count -gt 1) { $Commit = $parts[1] }
}
Write-Host "==> StreamFinder $Version ($Commit)"

if (Test-Path $Out) { Remove-Item -Recurse -Force $Out }
New-Item -ItemType Directory -Force $Out, $Downloads | Out-Null

# Python: the official embeddable package, with the app folder and site-packages on its path.
$embed = Get-Download "https://www.python.org/ftp/python/$PythonVersion/python-$PythonVersion-embed-amd64.zip" "python-$PythonVersion-embed-amd64.zip"
Expand-Archive $embed (Join-Path $Out 'python')
$pth = Get-ChildItem (Join-Path $Out 'python\python*._pth') | Select-Object -First 1
$zip = (Get-ChildItem (Join-Path $Out 'python\python*.zip') | Select-Object -First 1).Name
Set-Content -Encoding ascii $pth.FullName "$zip`r`n.`r`nLib\site-packages`r`n..\app`r`nimport site"

Write-Host '==> Installing Python packages'
python -m pip install -q --disable-pip-version-check --no-compile `
  --target (Join-Path $Out 'python\Lib\site-packages') -r (Join-Path $Repo 'requirements.txt')
Assert-Ok 'pip install'

Write-Host '==> Copying the app'
$app = Join-Path $Out 'app'
New-Item -ItemType Directory -Force (Join-Path $app 'windows'), (Join-Path $app 'bin') | Out-Null
Copy-Item (Join-Path $Repo 'app.py'), (Join-Path $Repo '.env.example'), (Join-Path $Repo 'requirements.txt') $app
Copy-Item -Recurse (Join-Path $Repo 'streamfinder') $app
Get-ChildItem -Recurse -Directory -Filter __pycache__ $app | Remove-Item -Recurse -Force
foreach ($file in 'control.py', 'streamfinder.cmd', 'streamfinder.ico') {
  Copy-Item (Join-Path $Repo "windows\$file") (Join-Path $app 'windows')
}
Set-Content -Encoding ascii (Join-Path $app 'VERSION') "$Version $Commit"

$torr = Get-Download "https://github.com/YouROK/TorrServer/releases/download/$TorrServerTag/TorrServer-windows-amd64.exe" "TorrServer-$TorrServerTag.exe"
Copy-Item $torr (Join-Path $app 'bin\torrserver.exe')
Set-Content -Encoding ascii -NoNewline (Join-Path $app 'bin\torrserver.exe.version') $TorrServerTag

# Whisper for AI subtitles: the newest whisper.cpp release that has Windows binaries.
$releases = Invoke-RestMethod 'https://api.github.com/repos/ggml-org/whisper.cpp/releases?per_page=20' -Headers @{ 'User-Agent' = 'StreamFinder-build' }
$asset = $releases | ForEach-Object { $_.assets } | Where-Object { $_.name -eq 'whisper-bin-x64.zip' } | Select-Object -First 1
if (-not $asset) { throw 'No whisper.cpp release with whisper-bin-x64.zip' }
$whisperZip = Get-Download $asset.browser_download_url "whisper-$($asset.id).zip"
$whisperTmp = Join-Path $Downloads 'whisper-unpacked'
if (Test-Path $whisperTmp) { Remove-Item -Recurse -Force $whisperTmp }
Expand-Archive $whisperZip $whisperTmp
$bin = Join-Path $Out 'whisper\build\bin'
New-Item -ItemType Directory -Force $bin, (Join-Path $Out 'whisper\models') | Out-Null
Get-ChildItem -Recurse $whisperTmp -Include 'whisper-server.exe', '*.dll' | Copy-Item -Destination $bin
$model = Get-Download 'https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-tiny-q5_1.bin' 'ggml-tiny-q5_1.bin'
Copy-Item $model (Join-Path $Out 'whisper\models')

Write-Host '==> Building the setup'
$iscc = (Get-Command iscc -ErrorAction SilentlyContinue).Source
if (-not $iscc) { $iscc = "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe" }
& $iscc /Q "/DVersion=$Version" "/DSource=$Out" "/O$(Join-Path $Repo 'dist')" (Join-Path $PSScriptRoot 'installer.iss')
Assert-Ok 'Inno Setup'
Get-ChildItem (Join-Path $Repo 'dist\*.exe') | ForEach-Object { Write-Host "Built $($_.FullName)" }
