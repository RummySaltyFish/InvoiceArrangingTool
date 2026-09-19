param(
    [string]$Python = ""
)

$ErrorActionPreference = "Stop"
$root = $PSScriptRoot

if (-not $Python) {
    $portablePython = Join-Path $root ".build\python31315\python.exe"
    $Python = if (Test-Path -LiteralPath $portablePython) { $portablePython } else { "python" }
}

$versionText = & $Python -c "import sys; print('.'.join(map(str, sys.version_info[:3])))"
if ($LASTEXITCODE -ne 0) {
    throw "找不到可用的 Python：$Python"
}
$version = [version]$versionText.Trim()
if ($version -lt [version]"3.11" -or $version -eq [version]"3.13.0") {
    throw "请使用 Python 3.11 以上版本；Windows Python 3.13.0 存在 Tkinter 打包缺陷，请升级到 3.13.1 以上。"
}

$venv = Join-Path $root ".build\release-venv"
$venvPython = Join-Path $venv "Scripts\python.exe"
if (-not (Test-Path -LiteralPath $venvPython)) {
    & $Python -m venv $venv
}

& $venvPython -m pip install --disable-pip-version-check -r (Join-Path $root "requirements-build.txt")
if ($LASTEXITCODE -ne 0) {
    throw "构建依赖安装失败。"
}

$work = Join-Path $root ".build\release-work"
$dist = Join-Path $root ".build\release-dist"
$package = Join-Path $root "报销工具"
$zip = Join-Path $root "报销工具.zip"

foreach ($path in @($work, $dist, $package)) {
    if (Test-Path -LiteralPath $path) {
        $resolved = (Resolve-Path -LiteralPath $path).Path
        if (-not $resolved.StartsWith($root + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
            throw "拒绝删除项目目录外的路径：$resolved"
        }
        Remove-Item -LiteralPath $resolved -Recurse -Force
    }
}
Remove-Item -LiteralPath $zip -Force -ErrorAction SilentlyContinue

& $venvPython -m PyInstaller --noconfirm --clean `
    --distpath $dist --workpath $work (Join-Path $root "发票报销工具.spec")
if ($LASTEXITCODE -ne 0) {
    throw "程序打包失败。"
}

$built = Join-Path $dist "发票报销工具"
Copy-Item -LiteralPath $built -Destination $package -Recurse
Copy-Item -LiteralPath (Join-Path $root "使用说明.txt") -Destination (Join-Path $package "使用说明.txt") -Force
Compress-Archive -LiteralPath $package -DestinationPath $zip -CompressionLevel Optimal

$zipFile = Get-Item -LiteralPath $zip
Write-Output "打包完成：$($zipFile.FullName)"
Write-Output ("压缩包大小：{0:N2} MiB" -f ($zipFile.Length / 1MB))
