"""Read local Windows network configuration without sending traffic or changing settings."""
import json
import os
from pathlib import Path
import subprocess
import threading
import time


NETWORK_INTERVAL_S = 30
NETWORK_TIMEOUT_S = 5
# .NET reads adapter properties locally. No ping, DNS lookup, HTTP request, SSID or key collection.
NETWORK_COMMAND = r"""
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
$cards = @([System.Net.NetworkInformation.NetworkInterface]::GetAllNetworkInterfaces() | ForEach-Object {
    $card = $_
    $kind = $card.NetworkInterfaceType.ToString()
    if ($kind -ne 'Loopback' -and $kind -ne 'Tunnel') {
        $properties = $null
        $interfaceError = ''
        try { $properties = $card.GetIPProperties() } catch { $interfaceError = $_.Exception.Message }
        [pscustomobject]@{
            name = $card.Name
            description = $card.Description
            kind = $kind
            state = $card.OperationalStatus.ToString()
            addresses = @($properties.UnicastAddresses | ForEach-Object { if ($_ -ne $null) { $_.Address.ToString() } })
            gateways = @($properties.GatewayAddresses | ForEach-Object { if ($_ -ne $null) { $_.Address.ToString() } })
            dns = @($properties.DnsAddresses | ForEach-Object { if ($_ -ne $null) { $_.ToString() } })
            error = $interfaceError
        }
    }
})
[pscustomobject]@{
    available = [System.Net.NetworkInformation.NetworkInterface]::GetIsNetworkAvailable()
    interfaces = $cards
} | ConvertTo-Json -Depth 6 -Compress
"""


def spawn_snapshot():
    windows = Path(os.environ.get('SystemRoot', r'C:\Windows'))
    return subprocess.run([str(windows/'System32/WindowsPowerShell/v1.0/powershell.exe'),
        '-NoLogo','-NoProfile','-NonInteractive','-Command',NETWORK_COMMAND],
        stdin=subprocess.DEVNULL,capture_output=True,text=True,encoding='utf-8',errors='replace',
        creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0),timeout=NETWORK_TIMEOUT_S,check=True)


def read_network_snapshot():
    if os.name!='nt':
        raise OSError('本机网络配置检查目前仅支持 Windows')
    output = spawn_snapshot().stdout.lstrip('\ufeff').strip()
    snapshot = json.loads(output)
    if (not isinstance(snapshot,dict) or type(snapshot.get('available')) is not bool
            or not isinstance(snapshot.get('interfaces'),list)):
        raise ValueError('系统网络配置未返回预期格式')
    for card in snapshot['interfaces']:
        if (not isinstance(card,dict) or not all(isinstance(card.get(key),str) for key in ('name','kind','state'))
                or not all(isinstance(card.get(key),list) and all(isinstance(item,str) for item in card[key])
                           for key in ('addresses','gateways','dns'))):
            raise ValueError('系统网络接口信息不完整')
        for key in ('addresses','gateways','dns'):
            card[key] = sorted(set(card[key]))
    snapshot['interfaces'].sort(key=lambda item:(item['kind'],item['name']))
    return snapshot


def network_messages(snapshot):
    if snapshot.get('error'):
        return [('WARNING','网络状态检查失败：'+snapshot['error']+'；详情已记入后台日志。')]
    cards = snapshot['interfaces']
    messages = []
    for card in cards[:8]:
        if card.get('error'):
            messages.append(('WARNING',card['name']+'：读取接口配置失败；'+card['error']))
            continue
        kind = ('Wi-Fi' if card['kind']=='Wireless80211' else
                '以太网' if 'Ethernet' in card['kind'] else '网络接口')
        title = kind+' · '+card['name']
        if card['state']=='Up':
            ip = '、'.join(card['addresses'][:3]) or '尚未获得 IP'
            gateway = '、'.join(card['gateways'][:2]) or '未检测到默认网关'
            dns = '、'.join(card['dns'][:2]) or '未配置'
            messages.append(('INFO',f'{title}：链路已连接；IP {ip}；网关 {gateway}；DNS {dns}'))
        else:
            messages.append(('INFO',f'{title}：未连接（{card["state"]}）'))
    if len(cards)>8:
        messages.append(('INFO',f'另有 {len(cards)-8} 个接口，完整地址与 DNS 已记录到后台日志。'))
    if not any(card['state']=='Up' and card['addresses'] for card in cards):
        messages.append(('WARNING','未发现已连接且有 IP 的网络接口，请检查 Wi-Fi 或网线。'))
    messages.append(('INFO','本机网络检查完成；MES / OSS 可用性及鉴权以实际请求回执为准。'))
    return messages


class NetworkMonitor:
    def __init__(self, emit, stop, diagnostics, *, probe=read_network_snapshot, interval=NETWORK_INTERVAL_S):
        self.emit, self.stop, self.diagnostics = emit, stop, diagnostics
        self.probe, self.interval = probe, interval
        self.thread = None

    def start(self):
        self.thread = threading.Thread(target=self.run,name='network-status',daemon=True)
        self.thread.start()

    def run(self):
        previous = None
        while not self.stop.is_set():
            started = time.monotonic()
            try:
                snapshot = self.probe()
                error = None
            except Exception as exc:
                if isinstance(exc,subprocess.TimeoutExpired):
                    message=f'读取系统网络配置超时（{NETWORK_TIMEOUT_S} 秒）'
                elif isinstance(exc,subprocess.CalledProcessError):
                    message=f'系统网络查询失败（退出码 {exc.returncode}）：'+str(exc.stderr or '').strip()[:240]
                else:
                    message=str(exc) or type(exc).__name__
                snapshot, error = dict(error=message), exc
            if self.stop.is_set():
                return
            signature = json.dumps(snapshot,sort_keys=True,ensure_ascii=False)
            if signature != previous:
                self.diagnostics.station.event('environment.network.changed','本机网络状态',
                    level='WARNING' if error else 'INFO', error=error,snapshot=snapshot,
                    local_only=True,cloud_verified=False,stderr=str(getattr(error,'stderr','') or '')[:4096],
                    elapsed_s=round(time.monotonic()-started,3))
                self.emit(snapshot)
                previous = signature
            else:
                self.diagnostics.station.sample('environment.network.heartbeat',interval=300,
                    snapshot=snapshot,local_only=True,cloud_verified=False)
            self.stop.wait(self.interval)
