# Batch download: iterate tickers under workspace/portfolio and run dayu-cli download serially.
# Usage (from repo root):
#   powershell -NoProfile -ExecutionPolicy Bypass -File utils/download_all.ps1
#   utils\download_all.cmd
# Extra args are forwarded to download, e.g.:
#   utils\download_all.cmd --overwrite

param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]] $RemainingArgs
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Invoke-DayuDownload {
    # 调用 download 并写日志。Python INFO 常走 stderr，需避免被 PowerShell 当成终止错误。
    param(
        [Parameter(Mandatory = $true)]
        [string] $Ticker,
        [string[]] $ForwardArgs = @(),
        [Parameter(Mandatory = $true)]
        [string] $LogFile
    )

    $previousErrorAction = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        $output = & python -m dayu.cli download --ticker $Ticker @ForwardArgs 2>&1
        $output | Out-File -FilePath $LogFile -Encoding utf8
        return [int]$LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $previousErrorAction
    }
}

$repoRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
Set-Location $repoRoot

$logRoot = Join-Path $repoRoot "workspace/tmp/download_logs"
New-Item -ItemType Directory -Force -Path $logRoot | Out-Null

$portfolioDir = Join-Path $repoRoot "workspace/portfolio"
if (-not (Test-Path -LiteralPath $portfolioDir)) {
    Write-Error "portfolio directory not found: $portfolioDir"
}

$tickers = @(
    Get-ChildItem -LiteralPath $portfolioDir -Directory |
        Where-Object { -not $_.Name.StartsWith(".") } |
        Select-Object -ExpandProperty Name |
        Sort-Object
)

$sum = $tickers.Count
if ($sum -eq 0) {
    Write-Host "[download] no ticker directories under portfolio, exit"
    exit 1
}

$i = 0
$totalStart = Get-Date

foreach ($ticker in $tickers) {
    $i++
    $logFile = Join-Path $logRoot "${ticker}_download.log"
    Write-Host "[downloading][$i/$sum][$ticker]"
    $tStart = Get-Date

    $forwardArgs = @($RemainingArgs)
    $exitCode = Invoke-DayuDownload -Ticker $ticker -ForwardArgs $forwardArgs -LogFile $logFile

    $sec = [int]((Get-Date) - $tStart).TotalSeconds
    Write-Host "[download][$i/$sum][$ticker] elapsed: ${sec}s, exit=${exitCode} | log: $logFile"
}

$totalSec = [int]((Get-Date) - $totalStart).TotalSeconds
Write-Host "[download] total elapsed: ${totalSec}s"
