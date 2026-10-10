# prepare_env.ps1 - create .env for the Stenograph Jitsi Windows deployment.
# ASCII only. Safe to re-run: an existing .env is never overwritten.
#   - detects this PC's LAN IPv4 address (override with: set JITSI_IP=...)
#   - generates random passwords for the XMPP components
$ErrorActionPreference = "Stop"
$dir = Split-Path -Parent $MyInvocation.MyCommand.Path
$tpl = Join-Path $dir ".env.template"
$envFile = Join-Path $dir ".env"

function New-Password {
    # 32 hex chars from two GUIDs (GUID v4 randomness)
    $u = ([guid]::NewGuid().ToString("N") + [guid]::NewGuid().ToString("N"))
    return $u.Substring(0, 32)
}

if (Test-Path $envFile) {
    Write-Host "existing .env found - kept as is"
    $pub = (Select-String -Path $envFile -Pattern '^PUBLIC_URL=' | Select-Object -First 1)
    if ($pub) { Write-Host ("URL: " + ($pub.Line -replace '^PUBLIC_URL=', '')) }
    exit 0
}

$ip = $env:JITSI_IP
if (-not $ip) {
    # Prefer a private-range address on a real adapter (VPN/virtual adapters
    # such as Radmin, Hamachi, OpenVPN, VMware, WSL are ignored).
    $all = Get-NetIPConfiguration | Where-Object {
        $_.IPv4Address -and $_.InterfaceAlias -notmatch 'vEthernet|WSL|Docker|Loopback|Bluetooth|Radmin|Hamachi|OpenVPN|VMware|VirtualBox|Hyper-V'
    }
    $priv = $all | Where-Object { $_.IPv4Address.IPAddress -match '^(192\.168\.|10\.|172\.(1[6-9]|2\d|3[01])\.)' }
    $pick = $priv | Where-Object { $_.IPv4DefaultGateway } | Select-Object -First 1
    if (-not $pick) { $pick = $priv | Select-Object -First 1 }
    if (-not $pick) { $pick = $all | Where-Object { $_.IPv4DefaultGateway } | Select-Object -First 1 }
    if (-not $pick) { $pick = $all | Select-Object -First 1 }
    if ($pick) { $ip = $pick.IPv4Address.IPAddress }
}
if (-not $ip) {
    $ip = "127.0.0.1"
    Write-Host "WARNING: no LAN IPv4 address found, using 127.0.0.1"
    Write-Host "         Set JITSI_IP=<address> and delete .env to retry."
}
Write-Host ("host IP: " + $ip)

$text = [System.IO.File]::ReadAllText($tpl, [System.Text.Encoding]::UTF8)
$text = $text.Replace('{{HOST_IP}}', $ip)
foreach ($name in 'JICOFO', 'JVB', 'JIGASI_XMPP', 'JIGASI_TRANSCRIBER', 'JIBRI_XMPP', 'JIBRI_RECORDER') {
    $text = $text.Replace('{{PW_' + $name + '}}', (New-Password))
}
if ($text -match '\{\{') {
    Write-Host "ERROR: unresolved placeholders remain - check .env.template"
    exit 1
}
[System.IO.File]::WriteAllText($envFile, $text, (New-Object System.Text.UTF8Encoding($false)))
Write-Host ".env created"

$pub = (Select-String -Path $envFile -Pattern '^PUBLIC_URL=' | Select-Object -First 1)
if ($pub) { Write-Host ("URL: " + ($pub.Line -replace '^PUBLIC_URL=', '')) }
exit 0
