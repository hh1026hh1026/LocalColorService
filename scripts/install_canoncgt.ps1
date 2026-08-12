param(
    [string]$PythonExe = "C:\Users\Administrator\.conda\envs\localcolor\python.exe"
)

$ErrorActionPreference = "Stop"
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$ModelRoot = Join-Path $ProjectRoot "models\canoncgt"
$SourceRoot = Join-Path $ModelRoot "CanonCGT"
$Archive = Join-Path $ModelRoot "pretrained_models.zip"
$Checkpoint = Join-Path $SourceRoot "pretrained\SSL_updated_251111.pth"
Set-Location -LiteralPath $ProjectRoot

New-Item -ItemType Directory -Force -Path $ModelRoot | Out-Null
if (-not (Test-Path -LiteralPath (Join-Path $SourceRoot "demo.py"))) {
    git clone --depth 1 https://github.com/Jinwon-Ko/CanonCGT.git $SourceRoot
}
& $PythonExe -m pip install -r (Join-Path $ProjectRoot "requirements-neural.txt")
if (-not (Test-Path -LiteralPath $Checkpoint)) {
    & $PythonExe -m gdown "https://drive.google.com/uc?id=1SqzCXjdJ95TAhDYY9Z4TaQPuoqlEyfkT" -O $Archive
    New-Item -ItemType Directory -Force -Path (Split-Path $Checkpoint) | Out-Null
    Expand-Archive -LiteralPath $Archive -DestinationPath (Split-Path $Checkpoint) -Force
}
& $PythonExe -c "from color_core.canoncgt_provider import canoncgt_runtime_status; print(canoncgt_runtime_status())"
