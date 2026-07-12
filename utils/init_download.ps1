# Initial download for explicit tickers (comma-separated CSV).
# Usage (from repo root):
#   powershell -NoProfile -ExecutionPolicy Bypass -File utils/init_download.ps1 "AAPL,MSFT,TSLA"
#   utils\init_download.cmd "AAPL,MSFT,TSLA"
# Extra args are forwarded to download, e.g.:
#   utils\init_download.cmd "600519,0700" --overwrite

param(
    [Parameter(Mandatory = $true, Position = 0)]
    [string] $TickersCsv,
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

$tickers = @(
    $TickersCsv -split "," |
        ForEach-Object { $_.Trim() } |
        Where-Object { $_ -ne "" }
)

$sum = $tickers.Count
if ($sum -eq 0) {
    Write-Host "Usage: init_download.ps1 <TICKERS_CSV>  e.g. init_download.ps1 'AAPL,MSFT,TSLA'"
    exit 1
}

$i = 0
$totalStart = Get-Date
$forwardArgs = @($RemainingArgs)

foreach ($ticker in $tickers) {
    $i++
    $logFile = Join-Path $logRoot "${ticker}_download.log"
    Write-Host "[downloading][$i/$sum][$ticker]"
    $tStart = Get-Date

    $exitCode = Invoke-DayuDownload -Ticker $ticker -ForwardArgs $forwardArgs -LogFile $logFile

    $sec = [int]((Get-Date) - $tStart).TotalSeconds
    Write-Host "[download][$i/$sum][$ticker] elapsed: ${sec}s, exit=${exitCode} | log: $logFile"
}

$totalSec = [int]((Get-Date) - $totalStart).TotalSeconds
Write-Host "[download] total elapsed: ${totalSec}s"
