param(
  [int]$TargetPid,
  [int]$TargetIndex = 0,
  [string]$TargetProcessName = "nwmain",
  [string]$GameExe = (Join-Path $PSScriptRoot "NWN Diamond\nwmain.exe"),
  [ValidateSet("Debug", "Release")]
  [string]$Configuration = "Release",
  [int]$Slot = 1,
  [switch]$NoLaunch,
  [switch]$ListTargets,
  [switch]$NoFire,
  [int]$PostInjectDelayMs = 750,
  [string]$PythonExe
)

$ErrorActionPreference = "Stop"

if ($Slot -lt 1 -or $Slot -gt 12) {
  throw "Slot must be between 1 and 12."
}

if ($TargetIndex -lt 0) {
  throw "TargetIndex must be zero or greater."
}

$buildScript = Join-Path $PSScriptRoot "SimKeysHook2\build.ps1"
$pythonInjectorScript = Join-Path $PSScriptRoot "inject_simkeys.py"
$clientScript = Join-Path $PSScriptRoot "simKeys_Client.py"
$dllPath = Join-Path $PSScriptRoot "SimKeysHook2\$Configuration\SimKeysHook2.dll"
$logRoot = Join-Path $PSScriptRoot "SimKeysHook2\logs"

New-Item -ItemType Directory -Force -Path $logRoot | Out-Null

function Quote-CommandArgument {
  param([string]$Value)

  if ($null -eq $Value) {
    return '""'
  }

  if ($Value -match '[\s"]') {
    return '"' + ($Value -replace '"', '\"') + '"'
  }

  return $Value
}

function Format-CommandLine {
  param(
    [Parameter(Mandatory = $true)]
    [string]$FilePath,
    [string[]]$Arguments = @()
  )

  return ((@($FilePath) + $Arguments | ForEach-Object { Quote-CommandArgument $_ }) -join ' ')
}

function Format-ArgumentString {
  param([string[]]$Arguments = @())

  return (($Arguments | ForEach-Object { Quote-CommandArgument $_ }) -join ' ')
}

function Read-OptionalRawText {
  param(
    [Parameter(Mandatory = $true)]
    [string]$Path
  )

  if (-not (Test-Path $Path)) {
    return ""
  }

  $content = Get-Content -LiteralPath $Path -Raw
  if ($null -eq $content) {
    return ""
  }

  return [string]$content
}

function Resolve-PythonInterpreter {
  param([string]$RequestedPath)

  if (-not [string]::IsNullOrWhiteSpace($RequestedPath)) {
    $resolved = Resolve-Path -LiteralPath $RequestedPath -ErrorAction Stop
    return [pscustomobject]@{
      Path = $resolved.Path
      Source = "explicit"
    }
  }

  $pyLauncher = Get-Command py -ErrorAction SilentlyContinue
  if ($null -ne $pyLauncher) {
    try {
      $launcherOutput = & $pyLauncher.Source -0p 2>$null
      foreach ($line in @($launcherOutput)) {
        $text = [string]$line
        if ([string]::IsNullOrWhiteSpace($text)) {
          continue
        }
        if ($text -notmatch '^\s*-V:([^\s]+)\s+\*?\s*(.+python(?:w)?\.exe)\s*$') {
          continue
        }
        $versionTag = $Matches[1]
        $candidatePath = $Matches[2].Trim()
        if ($versionTag -notlike "*-32") {
          continue
        }
        if (-not (Test-Path -LiteralPath $candidatePath)) {
          continue
        }
        return [pscustomobject]@{
          Path = $candidatePath
          Source = "py-launcher-x86"
        }
      }
    } catch {
      # Fall through to the default python resolution.
    }
  }

  $defaultPython = Get-Command python -ErrorAction Stop
  return [pscustomobject]@{
    Path = $defaultPython.Source
    Source = "default-python"
  }
}

function Test-InjectCompletion {
  param(
    [string]$InjectText,
    [string]$InjectorTraceText
  )

  $combined = @($InjectText, $InjectorTraceText) -join "`r`n"
  if ([string]::IsNullOrWhiteSpace($combined)) {
    return $false
  }

  return (
    $combined.Contains("[+] Injected SimKeysHook2") -or
    $combined.Contains("SimKeysHook2 was already initialized") -or
    $combined.Contains("remote init rc=0x00000001") -or
    $combined.Contains("remote init rc=0x00000002") -or
    $combined.Contains("remote init result=0x00000001") -or
    $combined.Contains("remote init result=0x00000002")
  )
}

function Invoke-CapturedProcess {
  param(
    [Parameter(Mandatory = $true)]
    [string]$Label,
    [Parameter(Mandatory = $true)]
    [string]$FilePath,
    [string[]]$Arguments = @(),
    [Parameter(Mandatory = $true)]
    [string]$OutFile,
    [switch]$AllowFailure
  )

  Write-Host ""
  Write-Host "== $Label ==" -ForegroundColor Cyan

  $stdoutFile = Join-Path $logRoot ([IO.Path]::GetFileNameWithoutExtension($OutFile) + "_stdout.txt")
  $stderrFile = Join-Path $logRoot ([IO.Path]::GetFileNameWithoutExtension($OutFile) + "_stderr.txt")
  if (Test-Path $stdoutFile) {
    Remove-Item -LiteralPath $stdoutFile -Force
  }
  if (Test-Path $stderrFile) {
    Remove-Item -LiteralPath $stderrFile -Force
  }

  $argumentString = Format-ArgumentString -Arguments $Arguments
  $proc = Start-Process `
    -FilePath $FilePath `
    -ArgumentList $argumentString `
    -NoNewWindow `
    -Wait `
    -PassThru `
    -RedirectStandardOutput $stdoutFile `
    -RedirectStandardError $stderrFile

  $stdout = Read-OptionalRawText -Path $stdoutFile
  $stderr = Read-OptionalRawText -Path $stderrFile
  $commandLine = Format-CommandLine -FilePath $FilePath -Arguments $Arguments
  $combined = @(
    "label: $Label"
    "timestamp: $(Get-Date -Format o)"
    "command: $commandLine"
    "exit_code: $($proc.ExitCode)"
    ""
    "[stdout]"
    $stdout.TrimEnd()
    ""
    "[stderr]"
    $stderr.TrimEnd()
  ) -join "`r`n"

  Set-Content -LiteralPath $OutFile -Value $combined -Encoding UTF8

  if (-not [string]::IsNullOrWhiteSpace($stdout)) {
    Write-Host $stdout.TrimEnd()
  }
  if (-not [string]::IsNullOrWhiteSpace($stderr)) {
    $stderrColor = if ($proc.ExitCode -eq 0) { "DarkYellow" } else { "Red" }
    Write-Host $stderr.TrimEnd() -ForegroundColor $stderrColor
  }

  Remove-Item -LiteralPath $stdoutFile, $stderrFile -Force -ErrorAction SilentlyContinue

  if ($proc.ExitCode -ne 0 -and -not $AllowFailure) {
    throw "$Label failed with exit code $($proc.ExitCode). Full output was written to '$OutFile'."
  }

  return [pscustomobject]@{
    ExitCode = $proc.ExitCode
    StdOut = $stdout
    StdErr = $stderr
    Combined = $combined
    OutFile = $OutFile
  }
}

function Get-TargetProcessInfo {
  param(
    [Parameter(Mandatory = $true)]
    [int]$ProcessId
  )

  try {
    $proc = Get-Process -Id $ProcessId -ErrorAction Stop
  } catch {
    throw "Target pid $ProcessId was not found. The process has probably exited; launch NWN again and use the current pid."
  }

  $cim = Get-CimInstance -ClassName Win32_Process -Filter "ProcessId = $ProcessId"

  return [pscustomobject]@{
    Process = $proc
    Cim = $cim
  }
}

function Normalize-TargetProcessName {
  param(
    [Parameter(Mandatory = $true)]
    [string]$ProcessName
  )

  $trimmed = $ProcessName.Trim()
  if ($trimmed.EndsWith(".exe", [System.StringComparison]::OrdinalIgnoreCase)) {
    return $trimmed.Substring(0, $trimmed.Length - 4)
  }

  return $trimmed
}

function Get-TargetCandidates {
  param(
    [Parameter(Mandatory = $true)]
    [string]$ProcessName
  )

  $normalizedName = Normalize-TargetProcessName -ProcessName $ProcessName
  $processes = @(Get-Process -Name $normalizedName -ErrorAction SilentlyContinue | Sort-Object StartTime -Descending)
  if ($processes.Count -eq 0) {
    return @()
  }

  $cims = @(Get-CimInstance -ClassName Win32_Process -Filter "Name = '$normalizedName.exe'" -ErrorAction SilentlyContinue)
  $cimById = @{}
  foreach ($cim in $cims) {
    $cimById[[int]$cim.ProcessId] = $cim
  }

  $index = 0
  $candidates = foreach ($proc in $processes) {
    $index += 1
    [pscustomobject]@{
      Index = $index
      Process = $proc
      Cim = $cimById[$proc.Id]
    }
  }

  return @($candidates)
}

function Write-TargetCandidates {
  param(
    [Parameter()]
    [AllowEmptyCollection()]
    [object[]]$Candidates,
    [Parameter(Mandatory = $true)]
    [string]$ProcessName
  )

  $normalizedName = Normalize-TargetProcessName -ProcessName $ProcessName
  if ($Candidates.Count -eq 0) {
    Write-Host "No running $normalizedName.exe processes were found." -ForegroundColor DarkYellow
    return
  }

  Write-Host "Discovered $($Candidates.Count) running $normalizedName.exe process(es):" -ForegroundColor Yellow
  foreach ($candidate in $Candidates) {
    $proc = $candidate.Process
    $cim = $candidate.Cim
    $title = if ([string]::IsNullOrWhiteSpace($proc.MainWindowTitle)) { "<no title>" } else { $proc.MainWindowTitle }
    $path = if ($null -ne $cim -and -not [string]::IsNullOrWhiteSpace($cim.ExecutablePath)) { $cim.ExecutablePath } else { "<unknown>" }
    Write-Host ("  [{0}] pid={1} started={2:yyyy-MM-dd HH:mm:ss} hwnd=0x{3:X8} title={4}" -f $candidate.Index, $proc.Id, $proc.StartTime, $proc.MainWindowHandle, $title)
    Write-Host ("      path={0}" -f $path)
  }
}

function Resolve-ExistingTarget {
  param(
    [int]$ExplicitPid,
    [Parameter(Mandatory = $true)]
    [string]$ProcessName,
    [int]$ProcessIndex = 0,
    [switch]$AllowNone
  )

  if ($ExplicitPid) {
    $info = Get-TargetProcessInfo -ProcessId $ExplicitPid
    return [pscustomobject]@{
      Source = "pid"
      Candidate = [pscustomobject]@{
        Index = 0
        Process = $info.Process
        Cim = $info.Cim
      }
      Candidates = @()
    }
  }

  $candidates = @(Get-TargetCandidates -ProcessName $ProcessName)
  if ($candidates.Count -eq 0) {
    if ($AllowNone) {
      return $null
    }
    throw "No running $(Normalize-TargetProcessName -ProcessName $ProcessName).exe processes were found."
  }

  if ($candidates.Count -eq 1) {
    return [pscustomobject]@{
      Source = "name-auto"
      Candidate = $candidates[0]
      Candidates = $candidates
    }
  }

  if ($ProcessIndex -gt 0) {
    if ($ProcessIndex -gt $candidates.Count) {
      Write-TargetCandidates -Candidates $candidates -ProcessName $ProcessName
      throw "TargetIndex $ProcessIndex is out of range for the discovered process list."
    }

    return [pscustomobject]@{
      Source = "name-index"
      Candidate = $candidates[$ProcessIndex - 1]
      Candidates = $candidates
    }
  }

  Write-TargetCandidates -Candidates $candidates -ProcessName $ProcessName
  throw "Multiple $(Normalize-TargetProcessName -ProcessName $ProcessName).exe processes were found. Re-run with -TargetIndex <n> or -TargetPid <pid>."
}

function Write-RunManifest {
  param(
    [Parameter(Mandatory = $true)]
    [int]$ProcessId,
    [Parameter(Mandatory = $true)]
    [string]$OutFile
  )

  $target = Get-TargetProcessInfo -ProcessId $ProcessId
  $proc = $target.Process
  $cim = $target.Cim
  $pythonBits = & $pythonExe -c "import struct; print(struct.calcsize('P') * 8)"
  $lines = @(
    "timestamp: $(Get-Date -Format o)"
    "cwd: $PWD"
    "configuration: $Configuration"
    "target_process_name: $(Normalize-TargetProcessName -ProcessName $TargetProcessName)"
    "target_index: $TargetIndex"
    "dll: $dllPath"
    "python_injector_script: $pythonInjectorScript"
    "python: $pythonExe"
    "python_source: $pythonSource"
    "python_bits: $($pythonBits | Select-Object -First 1)"
    "target_pid: $ProcessId"
    "target_name: $($proc.ProcessName)"
    "target_path: $($cim.ExecutablePath)"
    "target_command_line: $($cim.CommandLine)"
    "target_parent_pid: $($cim.ParentProcessId)"
    "target_start_time: $($cim.CreationDate)"
    "target_main_window_title: $($proc.MainWindowTitle)"
    ("target_main_window_handle: 0x{0:X8}" -f $proc.MainWindowHandle)
    "target_handle_count: $($proc.HandleCount)"
    "target_thread_count: $($proc.Threads.Count)"
    "target_working_set: $($proc.WorkingSet64)"
  )

  Set-Content -LiteralPath $OutFile -Value ($lines -join "`r`n") -Encoding UTF8
}

$pythonResolution = Resolve-PythonInterpreter -RequestedPath $PythonExe
$pythonExe = $pythonResolution.Path
$pythonSource = $pythonResolution.Source
$pythonBits = (& $pythonExe -c "import struct; print(struct.calcsize('P') * 8)" | Select-Object -First 1)

if ($ListTargets) {
  $candidates = @(Get-TargetCandidates -ProcessName $TargetProcessName)
  Write-TargetCandidates -Candidates $candidates -ProcessName $TargetProcessName
  return
}

Write-Host "Building SimKeysHook2 for live test..." -ForegroundColor Yellow
& $buildScript -Configuration $Configuration

if (-not (Test-Path $dllPath)) {
  throw "Built DLL was not found at '$dllPath'."
}

Write-Host "Using Python '$pythonExe' ($pythonBits-bit) via $pythonSource." -ForegroundColor Yellow
if ($pythonBits -ne "32") {
  throw "The selected Python interpreter is $pythonBits-bit. SimKeys injection requires a 32-bit Python interpreter."
}
if (-not (Test-Path $pythonInjectorScript)) {
  throw "Python injector script was not found at '$pythonInjectorScript'."
}
Write-Host "Using Python injector path." -ForegroundColor Yellow

$targetStatusPrinted = $false

if (-not $TargetPid) {
  $resolved = Resolve-ExistingTarget -ProcessName $TargetProcessName -ProcessIndex $TargetIndex -AllowNone:(-not $NoLaunch)
  if ($null -ne $resolved) {
    $TargetPid = $resolved.Candidate.Process.Id
    $target = [pscustomobject]@{
      Process = $resolved.Candidate.Process
      Cim = $resolved.Candidate.Cim
    }
    Write-Host "Using discovered pid $TargetPid ($($target.Process.ProcessName)) via $($resolved.Source)." -ForegroundColor Green
    $targetStatusPrinted = $true
  }
}

if (-not $TargetPid) {
  if ($NoLaunch) {
    throw "No running $(Normalize-TargetProcessName -ProcessName $TargetProcessName).exe process was found, and -NoLaunch was specified."
  }

  if (-not (Test-Path $GameExe)) {
    throw "Game executable '$GameExe' was not found."
  }

  $gameDir = Split-Path $GameExe -Parent
  Write-Host "Launching NWN from '$GameExe'..." -ForegroundColor Yellow
  $proc = Start-Process -FilePath $GameExe -WorkingDirectory $gameDir -PassThru
  $TargetPid = $proc.Id

  try {
    $null = $proc.WaitForInputIdle(15000)
  } catch {
    Write-Host "WaitForInputIdle did not complete; continuing." -ForegroundColor DarkYellow
  }

  Write-Host "Game started with pid $TargetPid." -ForegroundColor Green
  Write-Host "Load into the game until the quickbar is visible, then press Enter to continue." -ForegroundColor Yellow
  [void](Read-Host)
} else {
  if ($null -eq $target) {
    $target = Get-TargetProcessInfo -ProcessId $TargetPid
  }
  if (-not $targetStatusPrinted) {
    Write-Host "Using existing pid $TargetPid ($($target.Process.ProcessName))." -ForegroundColor Green
  }
  if ($target.Process.ProcessName -notlike "$(Normalize-TargetProcessName -ProcessName $TargetProcessName)*") {
    Write-Host "The target process name does not look like NWN. Continuing anyway for diagnostics." -ForegroundColor DarkYellow
  }
}

$timestamp = Get-Date -Format "yyyyMMdd_HHmmss"
$manifestOut = Join-Path $logRoot "live_${timestamp}_pid${TargetPid}_manifest.txt"
$injectOut = Join-Path $logRoot "live_${timestamp}_pid${TargetPid}_inject.txt"
$setLogOut = Join-Path $logRoot "live_${timestamp}_pid${TargetPid}_setlog.txt"
$preSnapshotOut = Join-Path $logRoot "live_${timestamp}_pid${TargetPid}_pre_snapshot.txt"
$preQueryOut = Join-Path $logRoot "live_${timestamp}_pid${TargetPid}_pre_query.txt"
$slotOut = Join-Path $logRoot "live_${timestamp}_pid${TargetPid}_slot${Slot}.txt"
$postSnapshotOut = Join-Path $logRoot "live_${timestamp}_pid${TargetPid}_post_snapshot.txt"
$postQueryOut = Join-Path $logRoot "live_${timestamp}_pid${TargetPid}_post_query.txt"
$hookLog = Join-Path $logRoot "simkeys_${TargetPid}.log"
$injectorTrace = $null
$injectorTraceText = ""
$stepFailures = @()
$targetAliveAfterInject = $true
$quickbarCaptured = $false

Write-RunManifest -ProcessId $TargetPid -OutFile $manifestOut

Invoke-CapturedProcess -Label "Inject DLL" -FilePath $pythonExe -Arguments @($pythonInjectorScript, "--pid", "$TargetPid", "--dll", $dllPath) -OutFile $injectOut | Out-Null
$injectorTrace = Get-ChildItem -LiteralPath $logRoot -Filter "pyinject_${TargetPid}_*.log" -ErrorAction SilentlyContinue |
  Sort-Object LastWriteTime -Descending |
  Select-Object -First 1
if ($null -ne $injectorTrace) {
  $injectorTraceText = Read-OptionalRawText -Path $injectorTrace.FullName
}
if (-not (Test-InjectCompletion -InjectText (Read-OptionalRawText -Path $injectOut) -InjectorTraceText $injectorTraceText)) {
  $stepFailures += "Inject DLL -> $injectOut"
  Write-Host "Inject DLL did not reach a confirmed completion marker; continuing to collect diagnostics." -ForegroundColor DarkYellow
}
Start-Sleep -Milliseconds $PostInjectDelayMs

$targetAliveAfterInject = $null -ne (Get-Process -Id $TargetPid -ErrorAction SilentlyContinue)
if (-not $targetAliveAfterInject) {
  $stepFailures += "Target exited during or immediately after injection -> pid $TargetPid"
  Write-Host "Target pid $TargetPid is no longer running after injection; skipping pipe-based steps." -ForegroundColor DarkYellow
}

if ($targetAliveAfterInject) {
  $setLogResult = Invoke-CapturedProcess -Label "Set Debug Logging" -FilePath $pythonExe -Arguments @($clientScript, "--pid", "$TargetPid", "setlog", "2") -OutFile $setLogOut -AllowFailure
  if ($setLogResult.ExitCode -ne 0) {
    $stepFailures += "Set Debug Logging -> $setLogOut"
    Write-Host "Set Debug Logging failed; continuing to collect diagnostics." -ForegroundColor DarkYellow
  }

  $preSnapshotResult = Invoke-CapturedProcess -Label "Pre-Trigger Snapshot" -FilePath $pythonExe -Arguments @($clientScript, "--pid", "$TargetPid", "snapshot") -OutFile $preSnapshotOut -AllowFailure
  if ($preSnapshotResult.ExitCode -ne 0) {
    $stepFailures += "Pre-Trigger Snapshot -> $preSnapshotOut"
  }

  $preQueryResult = Invoke-CapturedProcess -Label "Pre-Trigger Query" -FilePath $pythonExe -Arguments @($clientScript, "--pid", "$TargetPid", "query") -OutFile $preQueryOut -AllowFailure
  if ($preQueryResult.ExitCode -ne 0) {
    $stepFailures += "Pre-Trigger Query -> $preQueryOut"
  } elseif ($preQueryResult.StdOut -match 'capturedThis=0x([0-9A-Fa-f]{8})') {
    try {
      $quickbarCaptured = [Convert]::ToUInt32($Matches[1], 16) -ne 0
    } catch {
      $quickbarCaptured = $false
    }
  }
} else {
  Set-Content -LiteralPath $setLogOut -Value "skipped: target process exited before pipe communication" -Encoding UTF8
  Set-Content -LiteralPath $preSnapshotOut -Value "skipped: target process exited before pipe communication" -Encoding UTF8
  Set-Content -LiteralPath $preQueryOut -Value "skipped: target process exited before pipe communication" -Encoding UTF8
}

if (-not $NoFire -and $targetAliveAfterInject) {
  Write-Host ""
  if (-not $quickbarCaptured) {
    Write-Host "Quickbar panel capture is still empty in the pre-trigger query." -ForegroundColor DarkYellow
    Write-Host "If slot activation does not fire, click a quickbar button or press the matching F-key once manually before continuing so the new traces can learn the live panel object." -ForegroundColor DarkYellow
    Write-Host ""
  }
  Write-Host "Leave the game focused or unfocused as needed, then press Enter to trigger quickbar slot $Slot." -ForegroundColor Yellow
  [void](Read-Host)
  $slotResult = Invoke-CapturedProcess -Label "Trigger Slot $Slot" -FilePath $pythonExe -Arguments @($clientScript, "--pid", "$TargetPid", "slot", "$Slot") -OutFile $slotOut -AllowFailure
  if ($slotResult.ExitCode -ne 0) {
    $stepFailures += "Trigger Slot $Slot -> $slotOut"
  }
  Start-Sleep -Milliseconds 250
  $postSnapshotResult = Invoke-CapturedProcess -Label "Post-Trigger Snapshot" -FilePath $pythonExe -Arguments @($clientScript, "--pid", "$TargetPid", "snapshot") -OutFile $postSnapshotOut -AllowFailure
  if ($postSnapshotResult.ExitCode -ne 0) {
    $stepFailures += "Post-Trigger Snapshot -> $postSnapshotOut"
  }
  $postQueryResult = Invoke-CapturedProcess -Label "Post-Trigger Query" -FilePath $pythonExe -Arguments @($clientScript, "--pid", "$TargetPid", "query") -OutFile $postQueryOut -AllowFailure
  if ($postQueryResult.ExitCode -ne 0) {
    $stepFailures += "Post-Trigger Query -> $postQueryOut"
  }
} elseif (-not $NoFire) {
  Set-Content -LiteralPath $slotOut -Value "skipped: target process exited before trigger" -Encoding UTF8
  Set-Content -LiteralPath $postSnapshotOut -Value "skipped: target process exited before trigger" -Encoding UTF8
  Set-Content -LiteralPath $postQueryOut -Value "skipped: target process exited before trigger" -Encoding UTF8
}

Write-Host ""
Write-Host "Live test files:" -ForegroundColor Green
Write-Host "  Manifest:   $manifestOut"
Write-Host "  DLL:        $dllPath"
Write-Host "  Inject log: $injectOut"
if ($null -ne $injectorTrace) {
  Write-Host "  Injector:   $($injectorTrace.FullName)"
}
Write-Host "  Set log:    $setLogOut"
Write-Host "  Pre snap:   $preSnapshotOut"
Write-Host "  Pre query:  $preQueryOut"
if (-not $NoFire) {
  Write-Host "  Slot log:   $slotOut"
  Write-Host "  Post snap:  $postSnapshotOut"
  Write-Host "  Post query: $postQueryOut"
}
Write-Host "  Hook log:   $hookLog"

if (-not (Test-Path $hookLog)) {
  Write-Host "The hook log does not exist yet. It should appear after the injected DLL writes its first log line." -ForegroundColor DarkYellow
}

if ($stepFailures.Count -gt 0) {
  Write-Host ""
  Write-Host "One or more live-test steps failed:" -ForegroundColor DarkYellow
  foreach ($failure in $stepFailures) {
    Write-Host "  $failure" -ForegroundColor DarkYellow
  }
  throw "Live test completed with one or more step failures. Review the generated logs for details."
}
