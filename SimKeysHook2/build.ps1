param(
  [ValidateSet("Debug", "Release")]
  [string]$Configuration = "Release"
)

$ErrorActionPreference = "Stop"

$vswhere = Join-Path ${env:ProgramFiles(x86)} "Microsoft Visual Studio\Installer\vswhere.exe"
if (-not (Test-Path $vswhere)) {
  throw "vswhere.exe was not found. Install Visual Studio 2022 Build Tools with the C++ workload first."
}

$installationPath = & $vswhere -latest -products * -requires Microsoft.VisualStudio.Workload.VCTools -property installationPath
if (-not $installationPath) {
  throw "No Visual Studio installation with the C++ Build Tools workload was found."
}

$msbuild = Join-Path $installationPath "MSBuild\Current\Bin\MSBuild.exe"
if (-not (Test-Path $msbuild)) {
  throw "MSBuild.exe was not found under '$installationPath'."
}

$project = Join-Path $PSScriptRoot "SimKeysHook2\SimKeysHook2.vcxproj"
if (-not (Test-Path $project)) {
  throw "Could not find '$project'."
}

$outDir = Join-Path $PSScriptRoot $Configuration
if (-not $outDir.EndsWith("\")) {
  $outDir += "\"
}
$outDirForMsbuild = $outDir -replace '\\', '/'

Write-Host "Building SimKeysHook2 ($Configuration|x86) with $msbuild"
& $msbuild $project /t:Rebuild "/p:Configuration=$Configuration;Platform=x86;OutDir=$outDirForMsbuild" /m /verbosity:minimal
exit $LASTEXITCODE
