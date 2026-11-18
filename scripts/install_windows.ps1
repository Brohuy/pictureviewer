[CmdletBinding()]
param(
    [string]$SourceExe = ""
)

$ErrorActionPreference = "Stop"

$AppName = "Picture Viewer"
$MenuLabel = "用 Picture Viewer 打开"

$repoRoot = Split-Path -Parent $PSScriptRoot
if (-not $SourceExe) {
    $candidates = @(
        (Join-Path $repoRoot "dist\PictureViewer.exe"),
        (Join-Path $repoRoot "PictureViewer.exe")
    )
    $SourceExe = $candidates | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
}
if (-not $SourceExe -or -not (Test-Path -LiteralPath $SourceExe)) {
    throw "找不到 PictureViewer.exe。请先运行 build_windows.bat，或使用 -SourceExe 指定路径。"
}
$SourceExe = (Resolve-Path -LiteralPath $SourceExe).Path

$installDir = Join-Path $env:LOCALAPPDATA "Programs\PictureViewer"
$targetExe = Join-Path $installDir "PictureViewer.exe"
New-Item -ItemType Directory -Force -Path $installDir | Out-Null

$running = Get-Process -Name "PictureViewer" -ErrorAction SilentlyContinue
if ($running) {
    throw "Picture Viewer 正在运行。请先关闭它，再重新执行安装。"
}

Copy-Item -LiteralPath $SourceExe -Destination $targetExe -Force

function Ensure-Key {
    param([Parameter(Mandatory = $true)][string]$Path)
    if (-not (Test-Path -LiteralPath $Path)) {
        New-Item -Path $Path -Force | Out-Null
    }
}

function Set-DefaultValue {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Value
    )
    Ensure-Key -Path $Path
    Set-ItemProperty -LiteralPath $Path -Name "(Default)" -Value $Value
}

function Set-StringValue {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Name,
        [Parameter(Mandatory = $true)][AllowEmptyString()][string]$Value
    )
    Ensure-Key -Path $Path
    New-ItemProperty -LiteralPath $Path -Name $Name -Value $Value -PropertyType String -Force | Out-Null
}

$quotedExe = '"' + $targetExe + '"'
$openCommand = $quotedExe + ' "%1"'
$backgroundCommand = $quotedExe + ' "%V"'
$iconValue = $quotedExe + ",0"

# Right-click on a folder.
$directoryVerb = "HKCU:\Software\Classes\Directory\shell\PictureViewer"
Set-DefaultValue -Path $directoryVerb -Value $MenuLabel
Set-StringValue -Path $directoryVerb -Name "Icon" -Value $iconValue
Set-StringValue -Path $directoryVerb -Name "MultiSelectModel" -Value "Single"
Set-DefaultValue -Path "$directoryVerb\command" -Value $openCommand

# Right-click on empty space inside a folder.
$backgroundVerb = "HKCU:\Software\Classes\Directory\Background\shell\PictureViewer"
Set-DefaultValue -Path $backgroundVerb -Value $MenuLabel
Set-StringValue -Path $backgroundVerb -Name "Icon" -Value $iconValue
Set-DefaultValue -Path "$backgroundVerb\command" -Value $backgroundCommand

# Refresh Explorer's context-menu cache.
Add-Type -Namespace PictureViewerNative -Name Shell32 -MemberDefinition @'
[System.Runtime.InteropServices.DllImport("shell32.dll")]
public static extern void SHChangeNotify(int eventId, uint flags, System.IntPtr item1, System.IntPtr item2);
'@
[PictureViewerNative.Shell32]::SHChangeNotify(0x08000000, 0, [IntPtr]::Zero, [IntPtr]::Zero) | Out-Null

Write-Host ""
Write-Host "Picture Viewer 已安装到:"
Write-Host "  $targetExe"
Write-Host ""
Write-Host "已添加右键菜单: $MenuLabel"
Write-Host "对文件夹和文件夹空白处均可用。"
Write-Host ""
