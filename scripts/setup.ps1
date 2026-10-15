param([string]$Root = (Split-Path -Parent $PSScriptRoot), [switch]$Offline)
$ErrorActionPreference = 'Stop'
$taskRoot = [IO.Path]::GetFullPath($Root).TrimEnd('\')
if ($taskRoot -eq [IO.Path]::GetPathRoot($taskRoot).TrimEnd('\')) { throw 'Use a dedicated application folder.' }
$taskLog = Join-Path $taskRoot ('logs\setup-' + (Get-Date -Format 'yyyyMMdd-HHmmss-ffff') + '.log')
$taskMarker = Join-Path $taskRoot 'data\setup-complete.json'
$taskEntryLock = $null
$taskTranscriptStarted = $false
$taskLauncherStarted = $false
function Write-SetupMarker($Value) {
    $taskPendingMarker = $taskMarker + '.new'
    [IO.File]::WriteAllText($taskPendingMarker, ($Value | ConvertTo-Json -Depth 12) + [Environment]::NewLine, [Text.UTF8Encoding]::new($false))
    if (Test-Path -LiteralPath $taskMarker) { [IO.File]::Replace($taskPendingMarker, $taskMarker, [NullString]::Value) }
    else { [IO.File]::Move($taskPendingMarker, $taskMarker) }
}
if ($taskRoot.Length -gt 40) {
    $taskReason = 'Application path is too long (' + $taskRoot.Length + '/40 UTF-16 units). Move the complete folder to E:\ROI-OCR and try again.'
    [IO.Directory]::CreateDirectory((Join-Path $taskRoot 'data')) | Out-Null
    Write-SetupMarker @{status='failed';app_name='drawing-roi-ocr';app_version='0.1.2';stage='root_path';reason=$taskReason;root_utf16_units=$taskRoot.Length;max_root_utf16_units=40}
    throw $taskReason
}
if (-not (Test-Path -LiteralPath (Join-Path $taskRoot 'source\run.py'))) { throw 'Application source is missing. Extract the complete ZIP.' }
foreach ($taskFolder in @('logs', 'data', 'temp')) { New-Item -ItemType Directory -Path (Join-Path $taskRoot $taskFolder) -Force | Out-Null }
try {
    $taskEntryLock = [IO.File]::Open((Join-Path $taskRoot 'data\setup-entry.lock'), 'OpenOrCreate', 'ReadWrite', 'None')
    $taskLauncherStarted = $true
    Write-SetupMarker @{ status = 'in_progress'; app_name = 'drawing-roi-ocr'; app_version = '0.1.2'; stage = 'python' }
    # Explicit loading avoids the observed Windows PowerShell 5 security-module autoload failure.
    Import-Module (Join-Path $PSHOME 'Modules\Microsoft.PowerShell.Security') -ErrorAction Stop
    Start-Transcript -LiteralPath $taskLog -ErrorAction Stop | Out-Null
    $taskTranscriptStarted = $true
    Write-Host 'Fixed official assets will be verified and installed inside this folder.'
    Write-Host 'Requirements: Windows x64, compatible NVIDIA GPU/driver, and installed DocuWorks.'
    Write-Host 'Third-party terms are listed in THIRD_PARTY_NOTICES.md.'
    # Check storage before creating Python; bootstrap repeats this after Python creation.
    $taskPackages = (Get-Content -LiteralPath (Join-Path $taskRoot 'assets\python-packages.json') -Raw | ConvertFrom-Json).packages
    $taskPythonAssets = (Get-Content -LiteralPath (Join-Path $taskRoot 'assets\python-runtime.json') -Raw | ConvertFrom-Json).assets
    $taskOcrAssets = Get-Content -LiteralPath (Join-Path $taskRoot 'assets\ocr-models.json') -Raw | ConvertFrom-Json
    $taskWheelBytes = [long]0; $taskPythonBytes = [long]0; $taskOcrBytes = [long]0; $taskMissingBytes = [long]0
    foreach ($taskAsset in $taskPackages) {
        $taskWheelBytes += [long]$taskAsset.bytes
        $taskPath = Join-Path $taskRoot ('downloads\wheels\' + $taskAsset.filename)
        if (-not (Test-Path -LiteralPath $taskPath -PathType Leaf) -or (Get-Item -LiteralPath $taskPath).Length -ne [long]$taskAsset.bytes) { $taskMissingBytes += [long]$taskAsset.bytes }
    }
    foreach ($taskAsset in $taskPythonAssets) {
        $taskPythonBytes += [long]$taskAsset.size
        $taskPath = Join-Path $taskRoot ('downloads\python\' + $taskAsset.name)
        if (-not (Test-Path -LiteralPath $taskPath -PathType Leaf) -or (Get-Item -LiteralPath $taskPath).Length -ne [long]$taskAsset.size) { $taskMissingBytes += [long]$taskAsset.size }
    }
    foreach ($taskAsset in $taskOcrAssets) {
        if ([long]$taskAsset.archive_bytes -le 0) { throw 'OCR archive size is missing.' }
        $taskOcrBytes += [long]$taskAsset.archive_bytes
        $taskName = ([Uri]$taskAsset.source).Segments[-1]
        $taskPath = Join-Path $taskRoot ('downloads\ocr\' + $taskName)
        if (-not (Test-Path -LiteralPath $taskPath -PathType Leaf) -or (Get-Item -LiteralPath $taskPath).Length -ne [long]$taskAsset.archive_bytes) { $taskMissingBytes += [long]$taskAsset.archive_bytes }
    }
    $taskRequired = $taskMissingBytes + 3*$taskWheelBytes + 2*$taskOcrBytes + 5*$taskPythonBytes + 1GB
    $taskDrive = [IO.DriveInfo]::new([IO.Path]::GetPathRoot($taskRoot))
    Write-Host ('Free space: {0:N2} GiB; required estimate: {1:N2} GiB' -f ($taskDrive.AvailableFreeSpace/1GB), ($taskRequired/1GB))
    if ($taskDrive.AvailableFreeSpace -lt $taskRequired) { throw 'Insufficient free space for downloads and extraction.' }
    & (Join-Path $taskRoot 'scripts\setup_python.ps1') -Root $taskRoot -ManifestPath (Join-Path $taskRoot 'assets\python-runtime.json') -Offline:$Offline
    if ($null -ne $LASTEXITCODE -and $LASTEXITCODE -ne 0) { throw 'Python setup failed.' }
    $taskPython = Join-Path $taskRoot 'runtime\python.exe'
    $taskArgs = @('-I', '-B', '-X', 'utf8', (Join-Path $taskRoot 'scripts\bootstrap.py'), '--root', $taskRoot)
    if ($Offline) { $taskArgs += '--offline' }
    $taskSavedErrorPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = 'Continue'
        & $taskPython @taskArgs 2>&1 | ForEach-Object { Write-Host $_ }
        $taskResult = $LASTEXITCODE
    } finally { $ErrorActionPreference = $taskSavedErrorPreference }
    if ($taskResult -ne 0) { throw ('Setup failed. Exit code: ' + $taskResult) }
} catch {
    if ($taskLauncherStarted) {
        $taskCurrent = if (Test-Path -LiteralPath $taskMarker) { Get-Content -LiteralPath $taskMarker -Raw | ConvertFrom-Json } else { $null }
        if (-not $taskCurrent -or $taskCurrent.status -eq 'in_progress') {
            Write-SetupMarker @{ status = 'failed'; app_name = 'drawing-roi-ocr'; app_version = '0.1.2'; stage = 'python_or_launcher'; reason = $_.Exception.Message }
        }
    }
    throw
} finally {
    if ($taskTranscriptStarted) { Stop-Transcript | Out-Null }
    if ($taskEntryLock) { $taskEntryLock.Dispose() }
    Write-Host ('Setup log: ' + $taskLog)
}
