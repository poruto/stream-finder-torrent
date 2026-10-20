# StreamFinder on Windows 10/11, one command in PowerShell:
#   irm https://raw.githubusercontent.com/poruto/stream-finder-torrent/master/windows-install.ps1 | iex
# or, inside a clone:  powershell -ExecutionPolicy Bypass -File windows-install.ps1
#
# Installs Git and Python when missing (winget), the app in its own Python
# environment, TorrServer and local Whisper, and runs them in the background from
# logon on (no window): restarted if they crash, updated from GitHub by themselves.
# Control it with:  streamfinder start | stop | restart | status | update | logs
# Run it again any time: it updates and only adds what is missing.

function Install-StreamFinder {
  $RepoUrl = 'https://github.com/poruto/stream-finder-torrent.git'
  $Dir = if ($env:STREAMFINDER_DIR) { $env:STREAMFINDER_DIR } else { Join-Path $HOME 'stream-finder-torrent' }
  # Run from a clone (development): use it as it is, never reset it.
  $InClone = $PSScriptRoot -and (Test-Path (Join-Path $PSScriptRoot 'windows\control.py'))
  if ($InClone) { $Dir = $PSScriptRoot }

  function Update-PathFromRegistry {
    $env:Path = [Environment]::GetEnvironmentVariable('Path', 'Machine') + ';' + [Environment]::GetEnvironmentVariable('Path', 'User')
  }

  function Install-WithWinget($id) {
    if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
      throw "winget is missing: install $id by hand (or 'App Installer' from the Microsoft Store), then run this again."
    }
    Write-Host "==> Installing $id"
    winget install -e --id $id --silent --accept-package-agreements --accept-source-agreements
    Update-PathFromRegistry
  }

  # The 'python' Microsoft Store alias is not a real Python: check that it runs.
  function Find-Python {
    $check = 'import sys; print(sys.executable) if sys.version_info >= (3, 10) else sys.exit(1)'
    $installed = @(Get-ChildItem "$env:LOCALAPPDATA\Programs\Python\Python3*\python.exe" -ErrorAction SilentlyContinue |
      Sort-Object FullName -Descending | ForEach-Object { $_.FullName })
    foreach ($command in @('py', 'python') + $installed) {
      if (-not (Get-Command $command -ErrorAction SilentlyContinue)) { continue }
      $arguments = if ($command -eq 'py') { @('-3', '-c', $check) } else { @('-c', $check) }
      $exe = & $command @arguments 2>$null
      if ($LASTEXITCODE -eq 0 -and $exe) { return ($exe | Select-Object -Last 1).Trim() }
    }
    return $null
  }

  if (-not (Get-Command git -ErrorAction SilentlyContinue)) { Install-WithWinget 'Git.Git' }
  if (-not (Get-Command git -ErrorAction SilentlyContinue)) { $env:Path += ';C:\Program Files\Git\cmd' }
  $Python = Find-Python
  if (-not $Python) { Install-WithWinget 'Python.Python.3.12'; $Python = Find-Python }
  if (-not $Python) { throw 'Python 3.10+ could not be found - install it from https://www.python.org and run this again.' }

  $Venv = Join-Path $Dir '.venv\Scripts'
  $Cli = Join-Path $Dir 'windows\control.py'
  # Running files can't be replaced: stop an earlier install first.
  if ((Test-Path "$Venv\python.exe") -and (Test-Path $Cli)) { & "$Venv\python.exe" $Cli stop }

  if (-not $InClone) {
    if (Test-Path (Join-Path $Dir '.git')) {
      Write-Host "==> Updating $Dir"
      git -C $Dir fetch -q origin master
      if ($LASTEXITCODE -ne 0) { throw 'git fetch failed' }
      git -C $Dir reset -q --hard FETCH_HEAD   # .env, cache/, data/ and bin/ are gitignored
    } else {
      Write-Host "==> Downloading StreamFinder into $Dir"
      git clone -q $RepoUrl $Dir
      if ($LASTEXITCODE -ne 0) { throw 'git clone failed' }
    }
  }

  if (-not (Test-Path "$Venv\python.exe")) {
    Write-Host '==> Creating the Python environment'
    & $Python -m venv (Join-Path $Dir '.venv')
    if ($LASTEXITCODE -ne 0) { throw 'python -m venv failed' }
  }
  Write-Host '==> Installing Python dependencies'
  & "$Venv\python.exe" -m pip install -q --disable-pip-version-check -r (Join-Path $Dir 'requirements.txt')
  if ($LASTEXITCODE -ne 0) { throw 'pip install failed' }

  # TorrServer, .env, missing keys (asked here), local Whisper.
  & "$Venv\python.exe" $Cli install
  if ($LASTEXITCODE -ne 0) { throw 'the install step failed' }

  Write-Host '==> Starting at logon'
  $startup = [Environment]::GetFolderPath('Startup')
  $shortcut = (New-Object -ComObject WScript.Shell).CreateShortcut((Join-Path $startup 'StreamFinder.lnk'))
  $shortcut.TargetPath = "$Venv\pythonw.exe"
  $shortcut.Arguments = "`"$Cli`" run"
  $shortcut.WorkingDirectory = $Dir
  $shortcut.Description = 'StreamFinder (TorrServer, app and Whisper in the background)'
  $shortcut.Save()

  # The `streamfinder` command in every new terminal.
  $commands = Join-Path $Dir 'windows'
  $userPath = [Environment]::GetEnvironmentVariable('Path', 'User')
  if (-not (($userPath -split ';') -contains $commands)) {
    [Environment]::SetEnvironmentVariable('Path', ("$userPath;$commands").TrimStart(';'), 'User')
  }
  if (-not (($env:Path -split ';') -contains $commands)) { $env:Path += ";$commands" }

  # Phones and TVs on the home network (Private network profile): one admin prompt.
  if (-not (Get-NetFirewallRule -DisplayName 'StreamFinder' -ErrorAction SilentlyContinue)) {
    Write-Host '==> Allowing StreamFinder through the firewall (Windows asks for permission)'
    $basePython = (& "$Venv\python.exe" -c 'import sys; print(sys._base_executable)').Trim()
    $torr = (Join-Path $Dir 'bin\torrserver.exe').Replace("'", "''")
    $py = $basePython.Replace("'", "''")
    $rules = "New-NetFirewallRule -DisplayName 'StreamFinder' -Direction Inbound -Program '$py' -Protocol TCP -LocalPort 5000 -Action Allow -Profile Private,Domain | Out-Null; " +
      "New-NetFirewallRule -DisplayName 'StreamFinder TorrServer' -Direction Inbound -Program '$torr' -Action Allow -Profile Private,Domain | Out-Null"
    $encoded = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($rules))
    try {
      Start-Process powershell -Verb RunAs -Wait -WindowStyle Hidden -ArgumentList '-NoProfile', '-EncodedCommand', $encoded
    } catch {
      Write-Host '!! Firewall not changed - other devices may not reach StreamFinder (this PC still works).'
    }
  }

  & "$Venv\python.exe" $Cli start
  Write-Host ''
  Write-Host 'StreamFinder now runs in the background from every logon, restarts if it crashes and updates itself.'
  Write-Host 'Other devices: the Wi-Fi/Ethernet network must be "Private" (Settings > Network & internet).'
  Write-Host 'Settings: login admin / admin (change it there). Command: streamfinder start|stop|restart|status|update|logs'
  Start-Process 'http://127.0.0.1:5000'
}

Install-StreamFinder
