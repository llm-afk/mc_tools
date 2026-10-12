$ErrorActionPreference = 'Stop'
$resultPath = 'C:\Users\32196\Desktop\mc_tools\cfw-uninstall-result.txt'
try {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($identity)
    if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) { throw 'Administrator permission is required.' }
    $targetPath = 'D:\Program Files\Clash For Windows\Clash.for.Windows-0.20.15-win'
    if (Test-Path -LiteralPath $targetPath) {
        $resolvedPath = (Resolve-Path -LiteralPath $targetPath).Path
        if ($resolvedPath -ne $targetPath) { throw 'Unexpected target path' }
        $processes = @(Get-CimInstance Win32_Process)
        $coreParents = @($processes | Where-Object { $_.Name -eq 'clash-core-service.exe' })
        $coreIds = @($coreParents | ForEach-Object { $_.ProcessId })
        $targets = @($processes | Where-Object {
            ($_.ExecutablePath -and $_.ExecutablePath.StartsWith($targetPath + '\', [StringComparison]::OrdinalIgnoreCase)) -or
            ($_.Name -eq 'clash-win64.exe' -and $_.ParentProcessId -in $coreIds)
        })
        $parentIds = @($targets | ForEach-Object { $_.ParentProcessId })
        $matchedParents = @($coreParents | Where-Object { $_.ProcessId -in $parentIds })
        $allTargets = @($matchedParents) + @($targets)
        $targetIds = @($allTargets | ForEach-Object { $_.ProcessId })
        $services = @(Get-CimInstance Win32_Service | Where-Object {
            ($_.ProcessId -ne 0 -and $_.ProcessId -in $targetIds) -or $_.PathName -like "*$targetPath*"
        })
        foreach ($service in $services) {
            Stop-Service -Name $service.Name -Force
            (Get-Service -Name $service.Name).WaitForStatus('Stopped', [TimeSpan]::FromSeconds(15))
        }
        foreach ($proc in $allTargets) {
            $live = Get-Process -Id $proc.ProcessId -ErrorAction SilentlyContinue
            if ($live) {
                Stop-Process -Id $proc.ProcessId -Force
                if (-not $live.WaitForExit(10000)) { throw ('Process did not exit: ' + $proc.Name) }
            }
        }
        foreach ($service in $services) {
            & sc.exe delete $service.Name | Out-Null
            if ($LASTEXITCODE -ne 0) { throw ('Service removal failed: ' + $service.Name) }
        }
        Start-Sleep -Seconds 2
        Remove-Item -LiteralPath $resolvedPath -Recurse -Force
    }
    $shortcutPath = 'C:\Users\32196\AppData\Roaming\Microsoft\Windows\Start Menu\Programs\Clash for Windows.lnk'
    if (Test-Path -LiteralPath $shortcutPath) { Remove-Item -LiteralPath $shortcutPath -Force }
    if ((Test-Path -LiteralPath $targetPath) -or (Test-Path -LiteralPath $shortcutPath)) { throw 'Removal verification failed.' }
    'SUCCESS: Clash for Windows removed. Configuration and Clash Verge preserved.' | Set-Content -LiteralPath $resultPath
} catch {
    'FAILED: ' + ($_ | Out-String) | Set-Content -LiteralPath $resultPath
    exit 1
}
