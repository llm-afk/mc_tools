[CmdletBinding()]
param(
    [ValidateSet('Inspect','Install')][string]$Mode='Inspect',
    [string]$DataRoot=(Join-Path $env:LOCALAPPDATA 'MirrorMe\FactoryStation'),
    [string]$ResultPath='',
    [string]$QueueName='Bar Code Printer T-4502 PRO',
    [ValidateSet('true','false')][string]$UseLocalPrinter='true'
)
$ErrorActionPreference='Stop'
[Console]::OutputEncoding=[Text.UTF8Encoding]::new($false)
$driverRoot=[IO.Path]::GetFullPath($PSScriptRoot)
$packages=Get-Content -Raw -LiteralPath (Join-Path $driverRoot 'packages.json') -Encoding UTF8 | ConvertFrom-Json
$script:events=[Collections.Generic.List[object]]::new()
$script:result=[ordered]@{schema_version=1;final=$false;ok=$false;events=$script:events;reboot_required=$false}
$script:extractRoot=$null
$script:delegated=$false

function Write-Receipt {
    if (-not $ResultPath) { return }
    $parent=[IO.Path]::GetDirectoryName([IO.Path]::GetFullPath($ResultPath))
    [IO.Directory]::CreateDirectory($parent) | Out-Null
    $temporary=Join-Path $parent ('.driver-result-'+[guid]::NewGuid().ToString('N')+'.tmp')
    [IO.File]::WriteAllText($temporary,($script:result | ConvertTo-Json -Depth 14 -Compress),[Text.UTF8Encoding]::new($false))
    if ([IO.File]::Exists($ResultPath)) { [IO.File]::Replace($temporary,$ResultPath,$null) }
    else { [IO.File]::Move($temporary,$ResultPath) }
}
function Add-Stage([string]$stage,[string]$message,$details=@{}) {
    $script:events.Add([ordered]@{at=[DateTimeOffset]::Now.ToString('o');stage=$stage;message=$message;details=$details})
    Write-Receipt
}
function Get-Inventory {
    $devices=@(Get-PnpDevice -PresentOnly -ErrorAction Stop)
    $pnp=@{}
    foreach ($row in @(Get-CimInstance Win32_PnPEntity)) { $pnp[$row.DeviceID]=$row }
    $signed=@{}
    foreach ($row in @(Get-CimInstance Win32_PnPSignedDriver)) { $signed[$row.DeviceID]=$row }
    $hardware=[Collections.Generic.List[object]]::new()
    $plan=[Collections.Generic.List[object]]::new()
    $issues=[Collections.Generic.List[string]]::new()
    foreach ($definition in $packages.usb) {
        $inf=Join-Path $driverRoot $definition.inf
        $ids=@($definition.hardware_ids)
        if (-not $ids.Count -or $null -eq $ids[0]) {
            $ids=@([regex]::Matches((Get-Content -Raw -LiteralPath $inf),
                 'USB\\VID_[0-9A-F]{4}&PID_[0-9A-F]{4}(?:&(?:REV_[0-9A-F]{4}|MI_[0-9A-F]{2}))*','IgnoreCase') |
                 ForEach-Object { $_.Value.ToUpperInvariant() } | Select-Object -Unique)
        }
        foreach ($device in $devices) {
            if ($device.InstanceId -notlike 'USB\*') { continue }
            $matches=@($ids | Where-Object { $device.InstanceId.StartsWith(($_+'\'),[StringComparison]::OrdinalIgnoreCase) })
            if (-not $matches.Count) {
                if ($device.InstanceId -notlike 'USB\VID_1A86*') { continue }
                $actual=@((Get-PnpDeviceProperty -InstanceId $device.InstanceId -KeyName DEVPKEY_Device_HardwareIds -ErrorAction SilentlyContinue).Data)
                $matches=@($ids | Where-Object { $actual -contains $_ })
            }
            if (-not $matches.Count) { continue }
            if (@($hardware | Where-Object { $_.instance_id -eq $device.InstanceId }).Count) { continue }
            $entry=$pnp[$device.InstanceId]
            $driver=$signed[$device.InstanceId]
            $problem=if ($entry) { [int]$entry.ConfigManagerErrorCode } else { if ($device.Status -eq 'OK') {0} else {-1} }
            $hardware.Add([ordered]@{name=$device.FriendlyName;instance_id=$device.InstanceId;device_class=$device.Class;
                service=$entry.Service;problem=$problem;driver_version=$driver.DriverVersion;driver_inf=$driver.InfName})
            if ($problem -eq 22) {
                $issues.Add($device.FriendlyName+' 已被禁用，请由工程人员核对。')
                continue
            }
            if ($problem -ne 0 -and $problem -ne 28) { $issues.Add($device.FriendlyName+' 的 Windows 设备状态异常，问题代码 '+$problem+'；请查看设备管理器。') }
            $wrongService=$definition.service -and $entry.Service -ne $definition.service
            if ($problem -eq 28 -or ($device.Class -ne $definition.device_class -and -not $definition.service) -or $wrongService) {
                $plan.Add([ordered]@{kind='usb';name=$definition.name;instance_id=$device.InstanceId;inf=$definition.inf})
            }
        }
    }
    $queues=@()
    $driverNames=@()
    $preferred=''
    $ports=@()
    $usb=@()
    if ($UseLocalPrinter -eq 'true') {
        $queues=@(Get-CimInstance Win32_Printer | ForEach-Object {
            [ordered]@{name=$_.Name;driver=$_.DriverName;port=$_.PortName;offline=[bool]$_.WorkOffline}
        })
        $driverNames=@(Get-PrinterDriver | ForEach-Object { $_.Name })
        $selected=@($queues | Where-Object { $_.name -eq $QueueName })
        $usb=@($devices | Where-Object {
            $_.InstanceId.StartsWith($packages.printer.usb_id,[StringComparison]::OrdinalIgnoreCase) -and
            ($_.FriendlyName -eq $packages.printer.model -or $_.Class -eq 'USB')
        })
        $ports=@($devices | Where-Object {
            $_.InstanceId -like 'USBPRINT\*' -and $_.FriendlyName -eq $packages.printer.model -and
            $_.InstanceId -match '(USB\d+)$'
        } | ForEach-Object { [regex]::Match($_.InstanceId,'(USB\d+)$').Groups[1].Value } | Select-Object -Unique)
        if ($selected.Count -and $usb.Count -and ($selected[0].driver -ne $packages.printer.model -or $selected[0].port -notin $ports)) {
            $issues.Add('配置的打印队列与本机标签打印机驱动或 USB 端口不匹配，请核对队列名称。')
        }
        if (-not $selected.Count -and $usb.Count) {
            $bound=@($queues | Where-Object { $_.port -in $ports -and $_.driver -eq $packages.printer.model })
            if ($bound.Count -eq 1 -and ($QueueName -eq $packages.printer.model -or -not $QueueName)) {
                $preferred=$bound[0].name
            } elseif ($ports.Count -eq 1) {
                $name=if ($QueueName) { $QueueName } else { $packages.printer.model }
                $plan.Add([ordered]@{kind='printer';name=$name;model=$packages.printer.model;port=$ports[0]})
            } else {
                $issues.Add('检测到标签打印机，但无法唯一确定 USB 打印端口，请工程人员核对。')
            }
        }
    }
    return [ordered]@{schema_version=1;devices=@($hardware.ToArray());queues=$queues;printer_drivers=$driverNames;
        plan=@($plan.ToArray());issues=@($issues.ToArray());preferred_printer=$preferred;printer_ports=$ports;
        printer_usb=@($usb | ForEach-Object { [ordered]@{instance_id=$_.InstanceId;name=$_.FriendlyName;device_class=$_.Class;status=$_.Status} })}
}


function Invoke-Native([string]$path,[string[]]$arguments) {
    Add-Stage 'command.start' '正在执行驱动安装程序。' @{file=$path;arguments=$arguments}
    $started=[DateTimeOffset]::Now
    $output=@(& $path @arguments 2>&1 | ForEach-Object { $_.ToString() })
    $code=$LASTEXITCODE
    Add-Stage 'command.finish' '驱动安装程序已返回，接下来核对设备。' @{file=$path;exit_code=$code;output=$output;duration_s=([DateTimeOffset]::Now-$started).TotalSeconds}
    if ($code -eq 3010) { $script:result.reboot_required=$true }
    elseif ($code -ne 0) { throw ('安装程序退出码 '+$code+'；'+($output -join ' ')) }
}
function Test-Package([string]$path) {
    $actual=(Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($actual -ne $packages.printer.sha256) { throw ('打印机驱动 SHA256 不匹配：'+$path) }
    $signature=Get-AuthenticodeSignature -LiteralPath $path
    if ($signature.Status -ne 'Valid') { throw ('打印机驱动数字签名无效：'+$signature.Status) }
    Add-Stage 'package.verified' '打印机官方驱动完整性和数字签名校验通过。' @{file=$path;sha256=$actual;signer=$signature.SignerCertificate.Subject;version=$packages.printer.version}
}
function Get-PrinterPackage {
    $bundled=Join-Path $driverRoot $packages.printer.bundled
    if (Test-Path -LiteralPath $bundled -PathType Leaf) { Test-Package $bundled; return $bundled }
    $cache=Join-Path $DataRoot 'driver-cache'
    [IO.Directory]::CreateDirectory($cache) | Out-Null
    $cached=Join-Path $cache $packages.printer.filename
    if (Test-Path -LiteralPath $cached -PathType Leaf) { Test-Package $cached; return $cached }
    $download=Join-Path $cache ('download-'+[guid]::NewGuid().ToString('N'))
    [IO.Directory]::CreateDirectory($download) | Out-Null
    try {
        $archive=Join-Path $download 'driver.zip'
        Add-Stage 'package.download' '交付目录未附带打印驱动，正在从厂商官网下载并缓存。' @{url=$packages.printer.url}
        [Net.ServicePointManager]::SecurityProtocol=[Net.SecurityProtocolType]::Tls12
        Invoke-WebRequest -UseBasicParsing -Uri $packages.printer.url -OutFile $archive -TimeoutSec 120 | Out-Null
        if ((Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash.ToLowerInvariant() -ne $packages.printer.archive_sha256) { throw '官网下载的打印机驱动压缩包 SHA256 不匹配。' }
        Expand-Archive -LiteralPath $archive -DestinationPath $download -Force
        $executable=Join-Path $download $packages.printer.filename
        Test-Package $executable
        [IO.File]::Move($executable,$cached)
        return $cached
    } finally {
        $full=[IO.Path]::GetFullPath($download)
        if ([IO.Path]::GetDirectoryName($full) -eq [IO.Path]::GetFullPath($cache) -and [IO.Path]::GetFileName($full).StartsWith('download-')) { Remove-Item -LiteralPath $full -Recurse -Force }
    }
}
function Install-Printer($task) {
    if (@(Get-PrinterDriver | Where-Object { $_.Name -eq $task.model }).Count) {
        Add-Stage 'queue.create' '型号驱动已存在，正在建立标签打印队列。' @{name=$task.name;model=$task.model;port=$task.port}
        Add-Printer -Name $task.name -DriverName $task.model -PortName $task.port
        return
    }
    $package=Get-PrinterPackage
    $script:extractRoot=Join-Path ([IO.Path]::GetTempPath()) ('pmt-driver-'+[guid]::NewGuid().ToString('N'))
    [IO.Directory]::CreateDirectory($script:extractRoot) | Out-Null
    Invoke-Native $package @('/x',$script:extractRoot)
    $wizard=Join-Path $script:extractRoot 'DriverWizard.exe'
    $signature=Get-AuthenticodeSignature -LiteralPath $wizard
    if ($signature.Status -ne 'Valid') { throw '解压后的官方 DriverWizard 数字签名无效。' }
    Invoke-Native $wizard @('install',('/name:'+ $task.name),('/model:'+ $task.model),('/port:'+ $task.port))
}
function Quote-PS([string]$value) { return "'"+$value.Replace("'","''")+"'" }


if ($Mode -eq 'Inspect') {
    try { Get-Inventory | ConvertTo-Json -Depth 12 -Compress; exit 0 }
    catch { @{schema_version=1;error=$_.Exception.Message} | ConvertTo-Json -Compress; exit 1 }
}
$exitCode=1
try {
    if (-not $ResultPath) {
        $ResultPath=Join-Path $DataRoot ('driver-setup/driver-'+[guid]::NewGuid().ToString('N')+'.json')
    }
    $ResultPath=[IO.Path]::GetFullPath($ResultPath)
    Add-Stage 'inspect' '正在核对本机设备驱动，已正常工作的驱动保持原样。'
    $before=Get-Inventory
    $script:result.before=$before
    Write-Receipt
    if ($before.plan.Count) {
        $principal=[Security.Principal.WindowsPrincipal]::new([Security.Principal.WindowsIdentity]::GetCurrent())
        if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
            Add-Stage 'elevation' '需要管理员安装设备驱动，请允许 Windows 管理员授权。' @{plan=$before.plan}
            $command='& '+(Quote-PS $PSCommandPath)+' -Mode Install -DataRoot '+(Quote-PS $DataRoot)+' -ResultPath '+(Quote-PS $ResultPath)+' -QueueName '+(Quote-PS $QueueName)+' -UseLocalPrinter '+(Quote-PS $UseLocalPrinter)
            $encoded=[Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($command))
            $process=Start-Process -FilePath (Join-Path $PSHOME 'powershell.exe') -ArgumentList @('-NoLogo','-NoProfile','-NonInteractive','-ExecutionPolicy','Bypass','-EncodedCommand',$encoded) -Verb RunAs -WindowStyle Hidden -Wait -PassThru
            $script:delegated=$true
            $exitCode=$process.ExitCode
        } else {
            foreach ($task in $before.plan) {
                Add-Stage 'install.start' ('正在准备 '+$task.name+'。') $task
                if ($task.kind -eq 'printer') { Install-Printer $task }
                elseif ($task.kind -eq 'usb') {
                    $inf=Join-Path $driverRoot $task.inf
                    Invoke-Native (Join-Path $env:SystemRoot 'System32/pnputil.exe') @('/add-driver',$inf,'/install')
                } else { throw '安装任务类型不受支持。' }
            }
            Add-Stage 'verify' '正在回读型号驱动、设备绑定和打印队列。'
            for ($attempt=1; $attempt -le 4; $attempt++) {
                $after=Get-Inventory
                $script:result.after=$after
                $remaining=@($after.plan)
                $failed=@($before.plan | Where-Object {
                $task=$_
                if ($task.kind -eq 'printer') {
                    -not @($after.queues | Where-Object { $_.name -eq $task.name -and $_.driver -eq $task.model -and $_.port -eq $task.port }).Count
                } else {
                    $device=@($after.devices | Where-Object { $_.instance_id -eq $task.instance_id })
                    -not $device.Count -or $device[0].problem -ne 0
                }
                })
                if (-not $remaining.Count -and -not $failed.Count) { break }
                Add-Stage 'verify.wait' '正在等待 Windows 更新设备绑定和打印队列。' @{attempt=$attempt;remaining=$remaining;unconfirmed=$failed}
                if ($attempt -lt 4) { Start-Sleep -Seconds 1 }
            }
            if ($remaining.Count -or $failed.Count) {
                $reason='安装程序已结束，但设备绑定或队列回读未通过；请查看日志中的安装前后快照。'
                if ($script:result.reboot_required) { $reason+='Windows 要求重启，请由现场负责人安排后重新检查。' }
                throw $reason
            }
            $script:result.ok=$true
            $exitCode=0
            Add-Stage 'complete' '驱动安装和回读确认完成，正在重新检查设备。'
        }
    } else {
        $script:result.ok=$true
        $exitCode=0
        Add-Stage 'complete' '当前已连接设备未发现需要补装的驱动。'
    }
} catch {
    $script:result.error=$_.Exception.Message
    $script:result.native_error=if ($_.Exception.PSObject.Properties['NativeErrorCode']) { $_.Exception.NativeErrorCode } else { $null }
    Add-Stage 'failed' '驱动准备未完成，请联系工程人员查看日志或运行根目录 install_drivers.cmd。' @{error=$_.Exception.Message;type=$_.Exception.GetType().FullName;native_error=$script:result.native_error}
} finally {
    if ($script:extractRoot) {
        $full=[IO.Path]::GetFullPath($script:extractRoot)
        $parent=[IO.Path]::GetFullPath([IO.Path]::GetTempPath()).TrimEnd('\')
        if ([IO.Path]::GetDirectoryName($full) -eq $parent -and [IO.Path]::GetFileName($full).StartsWith('pmt-driver-')) {
            Remove-Item -LiteralPath $full -Recurse -Force -ErrorAction SilentlyContinue
        }
    }
    if (-not $script:delegated) { $script:result.final=$true; Write-Receipt }
}
exit $exitCode
