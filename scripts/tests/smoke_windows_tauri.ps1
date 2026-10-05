# Runs only on a disposable GitHub Windows runner. Does not test the GUI or upload.
param([Parameter(Mandatory = $true)][string]$InstallRoot)

$ErrorActionPreference = 'Stop'
if ($env:GITHUB_ACTIONS -ne 'true') { throw 'This smoke test requires a disposable GitHub Actions runner' }

$installPath = (Resolve-Path -LiteralPath $InstallRoot).Path.TrimEnd([char[]]@('\', '/'))
$runnerTemp = (Resolve-Path -LiteralPath $env:RUNNER_TEMP).Path.TrimEnd([char[]]@('\', '/'))
if (-not $installPath.StartsWith($runnerTemp + '\', [StringComparison]::OrdinalIgnoreCase)) {
  throw "Refusing to launch outside RUNNER_TEMP: $installPath"
}
$exe = Join-Path $installPath 'aw-tauri.exe'
if (-not (Test-Path -LiteralPath $exe -PathType Leaf)) { throw "Missing Tauri executable: $exe" }

$ciProfile = "ci-$($env:GITHUB_RUN_ID)-$($env:GITHUB_RUN_ATTEMPT)"
if ($ciProfile -cnotmatch '^ci-[0-9]+-[0-9]+$' -or $ciProfile.Length -gt 32) {
  throw "Invalid isolated test profile: $ciProfile"
}

$portReservation = [Net.Sockets.TcpListener]::new([Net.IPAddress]::Loopback, 0)
try {
  $portReservation.Start()
  $port = ([Net.IPEndPoint]$portReservation.LocalEndpoint).Port
} finally {
  $portReservation.Stop()
}
$baseUrl = "http://127.0.0.1:$port/api/0"
$tauri = $null

try {
  # The install directory is deliberately NOT added to PATH: the patched
  # Tauri manager must discover its own bundled watchers from this location.
  $tauri = Start-Process -FilePath $exe -WorkingDirectory $installPath `
    -ArgumentList @('--daemon', '--profile', $ciProfile, '--port', "$port") `
    -PassThru -WindowStyle Hidden

  $deadline = [DateTime]::UtcNow.AddSeconds(90)
  $info = $null
  while ([DateTime]::UtcNow -lt $deadline) {
    $tauri.Refresh()
    if ($tauri.HasExited) { throw "Tauri exited before its server started: $($tauri.ExitCode)" }
    try {
      $info = Invoke-RestMethod -Uri "$baseUrl/info" -TimeoutSec 2
      break
    } catch {
      Start-Sleep -Milliseconds 1500
    }
  }
  if ($null -eq $info) { throw "Tauri did not serve $baseUrl/info within 90 seconds" }
  if ($info.profile -cne $ciProfile -or -not $info.hostname -or -not $info.device_id) {
    throw "Unexpected server identity at $baseUrl/info"
  }
  $listeners = @(Get-NetTCPConnection -State Listen -LocalPort $port -ErrorAction SilentlyContinue)
  if ($listeners.Count -eq 0 -or @($listeners | Where-Object OwningProcess -ne $tauri.Id).Count -gt 0) {
    throw "Server port $port does not belong exclusively to Tauri PID $($tauri.Id)"
  }

  $expected = @{
    'aw-watcher-afk.exe' = @{ client = 'aw-watcher-afk'; type = 'afkstatus' }
    'aw-watcher-window.exe' = @{ client = 'aw-watcher-window'; type = 'currentwindow' }
  }
  $verified = $false
  while ([DateTime]::UtcNow -lt $deadline) {
    $tauri.Refresh()
    if ($tauri.HasExited) { throw "Tauri exited before watchers registered: $($tauri.ExitCode)" }
    $children = @(Get-CimInstance Win32_Process -Filter "ParentProcessId = $($tauri.Id)")
    $buckets = Invoke-RestMethod -Uri "$baseUrl/buckets/" -TimeoutSec 3
    $entries = @($buckets.PSObject.Properties)
    $matchedBuckets = @{}
    $ready = $true

    foreach ($name in $expected.Keys) {
      $matchingProcesses = @($children | Where-Object { $_.Name -ieq $name })
      if ($matchingProcesses.Count -ne 1) { $ready = $false; break }
      $watcherPath = $matchingProcesses[0].ExecutablePath
      if (-not $watcherPath -or -not $watcherPath.StartsWith($installPath + '\', [StringComparison]::OrdinalIgnoreCase)) {
        throw "Watcher $name was not launched from the test installation"
      }

      $client = $expected[$name].client
      $kind = $expected[$name].type
      $bucketMatches = @($entries | Where-Object { $_.Value.client -eq $client -and $_.Value.type -eq $kind })
      if ($bucketMatches.Count -ne 1) { $ready = $false; break }
      $bucket = $bucketMatches[0].Value
      $expectedId = '{0}_{1}' -f $client, $bucket.hostname
      if ($bucketMatches[0].Name -cne $expectedId -or $bucket.hostname -ine $info.hostname) {
        throw "Watcher $client registered a bucket for a different host or identity"
      }
      $matchedBuckets[$name] = $bucketMatches[0].Name
    }

    if ($ready) { $verified = $true; break }
    Start-Sleep -Seconds 2
  }
  if (-not $verified) { throw 'Bundled window and AFK watchers did not register within 90 seconds' }

  $start = [Uri]::EscapeDataString([DateTimeOffset]::UtcNow.AddMinutes(-5).ToString('o'))
  $end = [Uri]::EscapeDataString([DateTimeOffset]::UtcNow.ToString('o'))
  foreach ($bucketId in $matchedBuckets.Values) {
    $encodedId = [Uri]::EscapeDataString($bucketId)
    $response = Invoke-WebRequest -Uri "$baseUrl/buckets/$encodedId/events?start=$start&end=$end" -TimeoutSec 10
    $document = [System.Text.Json.JsonDocument]::Parse($response.Content)
    try {
      if ($document.RootElement.ValueKind -ne [System.Text.Json.JsonValueKind]::Array) {
        throw "Events for $bucketId were not a JSON array"
      }
    } finally {
      $document.Dispose()
    }
  }
  Write-Host "Verified isolated Tauri server PID $($tauri.Id), port $port and bundled watcher buckets"
} finally {
  if ($null -ne $tauri) {
    $tauri.Refresh()
    if (-not $tauri.HasExited) {
      $tauri.Kill($true)
      if (-not $tauri.WaitForExit(10000)) { throw "Tauri PID $($tauri.Id) did not exit" }
    }
    $tauri.Dispose()
  }
}
