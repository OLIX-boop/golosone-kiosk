# Crea sul desktop il collegamento "Cassa Golosone".
# Usa Python senza finestra nera (pyw/pythonw): niente prompt da chiudere o da toccare per sbaglio.
param([switch]$Admin)
$dir = Split-Path -Parent $MyInvocation.MyCommand.Path
$app = Join-Path $dir 'avvia.py'
$pyw = (Get-Command pyw.exe -ErrorAction SilentlyContinue).Source
if (-not $pyw) { $pyw = (Get-Command pythonw.exe -ErrorAction SilentlyContinue).Source }
$exe = Join-Path $dir 'dist\GolosoneKiosk.exe'
if ($pyw) {
    $target = $pyw; $arguments = '"' + $app + '"'; $wd = $dir
} elseif (Test-Path $exe) {
    $target = $exe; $arguments = ''; $wd = Split-Path $exe
} else {
    Write-Host 'ERRORE: Python non trovato e GolosoneKiosk.exe non presente.' -ForegroundColor Red
    exit 1
}
$desk = [Environment]::GetFolderPath('Desktop')
$path = Join-Path $desk 'Cassa Golosone.lnk'
$ws = New-Object -ComObject WScript.Shell
$lnk = $ws.CreateShortcut($path)
$lnk.TargetPath = $target
$lnk.Arguments = $arguments
$lnk.WorkingDirectory = $wd
$lnk.IconLocation = (Join-Path $dir 'golosone.ico') + ',0'
$lnk.Description = 'Avvia la cassa self-service Golosone'
$lnk.Save()
if ($Admin) {  # spunta "Esegui come amministratore" nel collegamento
    $b = [IO.File]::ReadAllBytes($path); $b[0x15] = $b[0x15] -bor 0x20; [IO.File]::WriteAllBytes($path, $b)
}
Write-Host ''
Write-Host "Collegamento creato: $path" -ForegroundColor Green
Write-Host "Avvia: $target $arguments"
