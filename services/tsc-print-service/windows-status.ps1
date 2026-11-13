$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
$rows = @(Get-CimInstance Win32_Printer -ErrorAction Stop | ForEach-Object {
    [ordered]@{name=$_.Name;portName=$_.PortName;driverName=$_.DriverName;
        workOffline=[bool]$_.WorkOffline;printerStatus=[int]$_.PrinterStatus;
        detectedErrorState=[int]$_.DetectedErrorState}
})
ConvertTo-Json -InputObject $rows -Depth 4 -Compress
