param(
  [string]$PythonExe,
  [string]$InjectPython,
  [Parameter(ValueFromRemainingArguments = $true, Position = 0)]
  [string[]]$GuiArgs
)

$ErrorActionPreference = "Stop"

function Resolve-AnyPython {
  param([string]$RequestedPath)

  if (-not [string]::IsNullOrWhiteSpace($RequestedPath)) {
    $resolved = Resolve-Path -LiteralPath $RequestedPath -ErrorAction Stop
    return [pscustomobject]@{
      Path = $resolved.Path
      Source = "explicit"
    }
  }

  $defaultPython = Get-Command python -ErrorAction Stop
  return [pscustomobject]@{
    Path = $defaultPython.Source
    Source = "default-python"
  }
}

function Resolve-X86Python {
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
      # fall through
    }
  }

  $programFilesX86 = ${env:ProgramFiles(x86)}
  $localAppData = $env:LOCALAPPDATA
  $candidates = @(
    (Join-Path $programFilesX86 "Python313-32\python.exe"),
    (Join-Path $programFilesX86 "Python312-32\python.exe"),
    (Join-Path $programFilesX86 "Python311-32\python.exe"),
    (Join-Path $localAppData "Programs\Python\Python313-32\python.exe"),
    (Join-Path $localAppData "Programs\Python\Python312-32\python.exe"),
    (Join-Path $localAppData "Programs\Python\Python311-32\python.exe")
  )

  foreach ($candidate in $candidates) {
    if ([string]::IsNullOrWhiteSpace($candidate)) {
      continue
    }
    if (-not (Test-Path -LiteralPath $candidate)) {
      continue
    }
    return [pscustomobject]@{
      Path = $candidate
      Source = "common-x86"
    }
  }

  return $null
}

$guiPython = Resolve-AnyPython -RequestedPath $PythonExe
$injectorPython = Resolve-X86Python -RequestedPath $InjectPython
$guiScript = Join-Path $PSScriptRoot "simkeys_gui.py"

Write-Host "Using GUI Python '$($guiPython.Path)' via $($guiPython.Source)." -ForegroundColor Cyan
if ($null -ne $injectorPython) {
  Write-Host "Using injection Python '$($injectorPython.Path)' via $($injectorPython.Source)." -ForegroundColor Cyan
} else {
  Write-Warning "No 32-bit Python interpreter was found automatically. Inject buttons may fail until one is provided via -InjectPython."
}

$argsToPass = @($guiScript)
if ($null -ne $injectorPython) {
  $argsToPass += @("--inject-python", $injectorPython.Path)
}
$argsToPass += $GuiArgs

& $guiPython.Path @argsToPass
exit $LASTEXITCODE
