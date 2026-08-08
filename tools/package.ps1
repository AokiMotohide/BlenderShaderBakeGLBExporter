param(
    [string]$BlenderPath = "C:\Program Files\Blender Foundation\Blender 5.1\blender.exe"
)

$ErrorActionPreference = "Stop"
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$sourceDir = Join-Path $repoRoot "addon\shader_bake_glb_exporter"
$distDir = Join-Path $repoRoot "dist"
$outputPath = Join-Path $distDir "shader_bake_glb_exporter-1.0.0.zip"

if (-not (Test-Path -LiteralPath $BlenderPath -PathType Leaf)) {
    throw "Blender 5.1.1 was not found: $BlenderPath"
}

$stageRoot = Join-Path ([System.IO.Path]::GetTempPath()) ("shader_bake_glb_package_" + [guid]::NewGuid().ToString("N"))
$stageSource = Join-Path $stageRoot "shader_bake_glb_exporter"
New-Item -ItemType Directory -Path $stageSource -Force | Out-Null
New-Item -ItemType Directory -Path $distDir -Force | Out-Null

try {
    Copy-Item -Path (Join-Path $sourceDir "*") -Destination $stageSource -Recurse -Force
    Copy-Item -LiteralPath (Join-Path $repoRoot "LICENSE") -Destination (Join-Path $stageSource "LICENSE") -Force

    & $BlenderPath --background --factory-startup --command extension validate $stageSource
    if ($LASTEXITCODE -ne 0) {
        throw "Blender Extension validation failed"
    }

    & $BlenderPath --background --factory-startup --command extension build --source-dir $stageSource --output-filepath $outputPath
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $outputPath -PathType Leaf)) {
        throw "Blender Extension build failed"
    }

    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $archive = [System.IO.Compression.ZipFile]::OpenRead($outputPath)
    try {
        $entries = @($archive.Entries | ForEach-Object { $_.FullName.Replace("\", "/") })
        foreach ($required in @("__init__.py", "blender_manifest.toml", "LICENSE")) {
            if ($entries -notcontains $required) {
                throw "Required ZIP entry is missing: $required"
            }
        }
        if ($entries | Where-Object { $_ -match "(^|/)__pycache__/|\.pyc$|(^|/)tests/" }) {
            throw "ZIP contains tests or cache files"
        }
    }
    finally {
        $archive.Dispose()
    }

    $hash = Get-FileHash -LiteralPath $outputPath -Algorithm SHA256
    [pscustomobject]@{
        ZipPath = $outputPath
        Sha256 = $hash.Hash
        Size = (Get-Item -LiteralPath $outputPath).Length
    }
}
finally {
    if (Test-Path -LiteralPath $stageRoot) {
        Remove-Item -LiteralPath $stageRoot -Recurse -Force
    }
}
