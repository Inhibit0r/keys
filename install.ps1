# keys: install into %USERPROFILE%\.local and add it to the user PATH. Safe to rerun.
# `keys use` itself writes the active key into the user environment on Windows.
$ErrorActionPreference = "Stop"
$python = if (Get-Command python -ErrorAction SilentlyContinue) { "python" }
          elseif (Get-Command py -ErrorAction SilentlyContinue) { "py" }
          else { throw "keys: Python 3.10+ is required" }
$dir = Join-Path $HOME ".local\share\keys"
$bin = Join-Path $HOME ".local\bin"
New-Item -ItemType Directory -Force -Path $dir, $bin | Out-Null
Invoke-WebRequest "https://raw.githubusercontent.com/Inhibit0r/keys/main/keys.py" -OutFile (Join-Path $dir "keys.py")
# OEM encoding: cmd.exe reads the wrapper in the console code page
Set-Content -Path (Join-Path $bin "keys.cmd") -Value "@$python -X utf8 `"$dir\keys.py`" %*" -Encoding Oem
$path = [Environment]::GetEnvironmentVariable("Path", "User")
if (($path -split ";") -notcontains $bin) {
    [Environment]::SetEnvironmentVariable("Path", ($path.TrimEnd(";") + ";" + $bin), "User")
}
Write-Host "keys installed: $bin\keys.cmd"
Write-Host "open a new terminal and run: keys"
