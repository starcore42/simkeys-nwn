param(
  [string]$PythonExe,
  [string]$InjectPython,
  [Parameter(ValueFromRemainingArguments = $true, Position = 0)]
  [string[]]$GuiArgs
)

$ErrorActionPreference = "Stop"

function Test-IsAdministrator {
  $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
  $principal = New-Object Security.Principal.WindowsPrincipal $identity
  return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Quote-ProcessArgument {
  param([AllowEmptyString()][string]$Value)

  if ($null -eq $Value) {
    return '""'
  }

  $escaped = $Value.Replace('`', '``').Replace('"', '`"')
  return '"' + $escaped + '"'
}

function Start-SelfElevated {
  $arguments = @(
    "-NoProfile",
    "-ExecutionPolicy",
    "Bypass",
    "-File",
    (Quote-ProcessArgument -Value $PSCommandPath)
  )

  if (-not [string]::IsNullOrWhiteSpace($PythonExe)) {
    $arguments += @("-PythonExe", (Quote-ProcessArgument -Value $PythonExe))
  }

  if (-not [string]::IsNullOrWhiteSpace($InjectPython)) {
    $arguments += @("-InjectPython", (Quote-ProcessArgument -Value $InjectPython))
  }

  foreach ($arg in @($GuiArgs)) {
    $arguments += (Quote-ProcessArgument -Value $arg)
  }

  Write-Host "HGCC GUI needs administrator access to interact with elevated NWN clients. Requesting elevation..." -ForegroundColor Yellow
  Start-Process -FilePath "powershell.exe" `
    -Verb RunAs `
    -WorkingDirectory $PSScriptRoot `
    -ArgumentList ($arguments -join " ")
}

if (-not (Test-IsAdministrator)) {
  Start-SelfElevated
  exit 0
}

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
  throw "Could not find HGCC source directory '$srcPath'."
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
