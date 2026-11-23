$ErrorActionPreference = 'Stop'

$scannerWsl = Join-Path $env:WINDIR 'System32\wsl.exe'
$scannerNetsh = Join-Path $env:WINDIR 'System32\netsh.exe'

$runningDistros = & $scannerWsl --list --running --quiet
if ($LASTEXITCODE -ne 0) {
    throw 'Could not list running WSL distributions.'
}

# WSL can emit UTF-16 output with embedded nulls in Windows PowerShell.
$runningDistros = $runningDistros | ForEach-Object { ($_ -replace '[\x00\uFEFF]', '').Trim() }
if ($runningDistros -notcontains 'kiri') {
    return
}

$addressOutput = & $scannerWsl -d kiri -- ip -4 -o addr show dev eth0 scope global
if ($LASTEXITCODE -ne 0) {
    throw 'Could not read the Kiri IPv4 address.'
}

$addressMatch = [regex]::Match(($addressOutput -join ' '), '\binet (\d+(?:\.\d+){3})/')
if (-not $addressMatch.Success) {
    return
}
$kiriAddress = $addressMatch.Groups[1].Value

foreach ($proxyType in @('v4tov4', 'v6tov4')) {
    $listenAddress = if ($proxyType -eq 'v4tov4') { '0.0.0.0' } else { '::' }
    $registryKey = "HKLM:\SYSTEM\CurrentControlSet\Services\PortProxy\$proxyType\tcp"

    foreach ($port in @(9443, 8000)) {
        $ruleName = "$listenAddress/$port"
        $target = "$kiriAddress/$port"
        $currentRule = Get-ItemProperty -LiteralPath $registryKey -Name $ruleName -ErrorAction SilentlyContinue
        if ($currentRule.$ruleName -eq $target) {
            continue
        }

        $verb = if ($currentRule) { 'set' } else { 'add' }
        & $scannerNetsh interface portproxy $verb $proxyType "listenaddress=$listenAddress" "listenport=$port" "connectaddress=$kiriAddress" "connectport=$port"
        if ($LASTEXITCODE -ne 0) {
            throw "Could not update scanner port $port ($proxyType)."
        }
    }
}
