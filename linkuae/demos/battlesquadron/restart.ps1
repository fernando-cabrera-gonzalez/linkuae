# Restarts WinUAE with LinkUAE for the console loop of bot.py: boots the disk in bsquad.uae, or a savestate of yours.
#   restart.ps1 [-State path\to\file.uss] [-Exe path\to\winuae64.exe]   (default: the WINUAE variable, then linkuae\build\out)
param(
    [string]$State = "",
    [string]$Exe = $(if ($env:WINUAE) { $env:WINUAE } else { "$PSScriptRoot\..\..\build\out\winuae64.exe" })
)
$exe = (Resolve-Path $Exe -ErrorAction Stop).Path
$arguments = "-f `"$PSScriptRoot\bsquad.uae`""
if ($State) {
    $arguments += " -s `"statefile=$((Resolve-Path $State -ErrorAction Stop).Path)`""    # absolute: WinUAE runs from its own folder
}
Get-Process winuae64 -ErrorAction SilentlyContinue | Stop-Process -Force
Start-Sleep -Milliseconds 800
Start-Process -FilePath $exe -WorkingDirectory (Split-Path $exe) -ArgumentList $arguments | Out-Null
$py = "import sys,time; sys.path.insert(0, r'$PSScriptRoot\..\..\python'); from linkuae.link import emulator_present as e; t=time.time()
while not e() and time.time()-t < 20: time.sleep(0.2)
print('ready' if e() else 'timeout')"
python -c $py
