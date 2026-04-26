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

  $defaultPython = Get-Command python -ErrorAction SilentlyContinue
  if ($null -ne $defaultPython) {
    return [pscustomobject]@{
      Path = $defaultPython.Source
      Source = "default-python"
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
        if (-not (Test-Path -LiteralPath $candidatePath)) {
          continue
        }
        return [pscustomobject]@{
          Path = $candidatePath
          Source = "py-launcher-$versionTag"
        }
      }
    } catch {
      # Fall through to the default python resolution.
    }
  }

  $commonCandidates = @()
  if (-not [string]::IsNullOrWhiteSpace($env:LOCALAPPDATA)) {
    $commonCandidates += @(
      (Join-Path $env:LOCALAPPDATA "Programs\Python\Python313\python.exe"),
      (Join-Path $env:LOCALAPPDATA "Programs\Python\Python312\python.exe"),
      (Join-Path $env:LOCALAPPDATA "Programs\Python\Python311\python.exe")
    )
  }
  if (-not [string]::IsNullOrWhiteSpace($env:ProgramFiles)) {
    $commonCandidates += @(
      (Join-Path $env:ProgramFiles "Python313\python.exe"),
      (Join-Path $env:ProgramFiles "Python312\python.exe"),
      (Join-Path $env:ProgramFiles "Python311\python.exe")
    )
  }
  $programFilesX86 = ${env:ProgramFiles(x86)}
  if (-not [string]::IsNullOrWhiteSpace($programFilesX86)) {
    $commonCandidates += @(
      (Join-Path $programFilesX86 "Python313-32\python.exe"),
      (Join-Path $programFilesX86 "Python312-32\python.exe"),
      (Join-Path $programFilesX86 "Python311-32\python.exe")
    )
  }

  foreach ($candidatePath in $commonCandidates) {
    if (-not (Test-Path -LiteralPath $candidatePath)) {
      continue
    }
    $source = "common-python"
    if ($candidatePath -match '\\Python\d+-32\\') {
      $source = "common-x86"
    }
    return [pscustomobject]@{
      Path = $candidatePath
      Source = $source
    }
  }

  throw "Could not find a usable Python interpreter."
}

$python = Resolve-PythonInterpreter -RequestedPath $PythonExe
Write-Host "Using Python '$($python.Path)' via $($python.Source)." -ForegroundColor Cyan

$repoRoot = Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")
$srcPath = Join-Path $repoRoot.Path "src"
if (-not (Test-Path -LiteralPath $srcPath)) {
  throw "Could not find HGCC source directory '$srcPath'."
}

$previousPythonPath = $env:PYTHONPATH
if ([string]::IsNullOrWhiteSpace($previousPythonPath)) {
  $env:PYTHONPATH = $srcPath
} else {
  $env:PYTHONPATH = "$srcPath;$previousPythonPath"
}

$exitCode = 0
try {
  & $python.Path -m simkeys_app.simkeys_control @ControlArgs
  $exitCode = $LASTEXITCODE
} finally {
  $env:PYTHONPATH = $previousPythonPath
}
exit $exitCode
