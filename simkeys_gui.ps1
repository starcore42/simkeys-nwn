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

function Resolve-ExplicitPython {
  param([string]$RequestedPath)

  if ([string]::IsNullOrWhiteSpace($RequestedPath)) {
    return $null
  }

  $resolved = Resolve-Path -LiteralPath $RequestedPath -ErrorAction Stop
  return [pscustomobject]@{
    Path = $resolved.Path
    Source = "explicit"
  }
}

$guiPython = Resolve-AnyPython -RequestedPath $PythonExe
$injectorPython = Resolve-ExplicitPython -RequestedPath $InjectPython
Write-Host "Using GUI Python '$($guiPython.Path)' via $($guiPython.Source)." -ForegroundColor Cyan
if ($null -ne $injectorPython) {
  Write-Host "Using alternate injection Python '$($injectorPython.Path)' via $($injectorPython.Source)." -ForegroundColor Cyan
} else {
  Write-Host "Using GUI Python for injection as well." -ForegroundColor Cyan
}

$srcPath = Join-Path $PSScriptRoot "src"
if (-not (Test-Path -LiteralPath $srcPath)) {
  throw "Could not find SimKeys source directory '$srcPath'."
}

$argsToPass = @("-m", "simkeys_app.simkeys_gui")
if ($null -ne $injectorPython) {
  $argsToPass += @("--inject-python", $injectorPython.Path)
}
$argsToPass += $GuiArgs

$previousPythonPath = $env:PYTHONPATH
if ([string]::IsNullOrWhiteSpace($previousPythonPath)) {
  $env:PYTHONPATH = $srcPath
} else {
  $env:PYTHONPATH = "$srcPath;$previousPythonPath"
}

$exitCode = 0
try {
  & $guiPython.Path @argsToPass
  $exitCode = $LASTEXITCODE
} finally {
  $env:PYTHONPATH = $previousPythonPath
}
exit $exitCode
