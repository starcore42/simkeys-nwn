param(
  [Parameter(ValueFromRemainingArguments = $true, Position = 0)]
  [string[]]$ControlArgs,
  [string]$PythonExe
)

$ErrorActionPreference = "Stop"

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

  $commonCandidates = @(
    "C:\Program Files (x86)\Python313-32\python.exe",
    "C:\Program Files (x86)\Python312-32\python.exe",
    "C:\Program Files (x86)\Python311-32\python.exe",
    (Join-Path $env:LOCALAPPDATA "Programs\Python\Python313-32\python.exe"),
    (Join-Path $env:LOCALAPPDATA "Programs\Python\Python312-32\python.exe"),
    (Join-Path $env:LOCALAPPDATA "Programs\Python\Python311-32\python.exe")
  ) | Where-Object { -not [string]::IsNullOrWhiteSpace($_) }

  foreach ($candidatePath in $commonCandidates) {
    if (-not (Test-Path -LiteralPath $candidatePath)) {
      continue
    }
    return [pscustomobject]@{
      Path = $candidatePath
      Source = "common-x86"
    }
  }

  $defaultPython = Get-Command python -ErrorAction Stop
  return [pscustomobject]@{
    Path = $defaultPython.Source
    Source = "default-python"
  }
}

$python = Resolve-PythonInterpreter -RequestedPath $PythonExe
$controller = Join-Path $PSScriptRoot "simkeys_control.py"

Write-Host "Using Python '$($python.Path)' via $($python.Source)." -ForegroundColor Cyan
if ($ControlArgs.Count -gt 0 -and @("inject-next", "inject-all") -contains $ControlArgs[0]) {
  $pointerSize = (& $python.Path -c "import ctypes; print(ctypes.sizeof(ctypes.c_void_p))" 2>$null | Select-Object -First 1)
  if ([string]$pointerSize -ne "4") {
    Write-Warning "The selected Python interpreter is not 32-bit. Inject commands will fail until an x86 Python is available or passed via -PythonExe."
  }
}
& $python.Path $controller @ControlArgs
exit $LASTEXITCODE
