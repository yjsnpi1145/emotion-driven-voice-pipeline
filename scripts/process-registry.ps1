Set-StrictMode -Version Latest

function Test-RecordedProcessIdentity {
  param(
    [Parameter(Mandatory = $true)]$Record
  )

  if ($null -eq $Record -or $null -eq $Record.pid -or $null -eq $Record.create_time) {
    return $false
  }
  $recordedPid = [int]$Record.pid
  $recordedCreateTime = [double]$Record.create_time
  if ($recordedPid -le 0 -or $recordedCreateTime -le 0) {
    return $false
  }
  try {
    $process = Get-Process -Id $recordedPid -ErrorAction Stop
    $epoch = [datetime]::new(1970, 1, 1, 0, 0, 0, [datetimekind]::Utc)
    $actualCreateTime = ($process.StartTime.ToUniversalTime() - $epoch).TotalSeconds
    return [math]::Abs($actualCreateTime - $recordedCreateTime) -lt 1.0
  } catch {
    return $false
  }
}

function Get-LiveRecordedWorkerPids {
  param(
    [Parameter(Mandatory = $true)]$RunPayload
  )

  $result = [System.Collections.Generic.List[int]]::new()
  if ($null -eq $RunPayload.workers) {
    return @($result)
  }
  foreach ($workerName in @('indextts', 'gpt_sovits')) {
    $property = $RunPayload.workers.PSObject.Properties[$workerName]
    if ($null -eq $property) {
      continue
    }
    $record = $property.Value
    if (Test-RecordedProcessIdentity -Record $record) {
      $result.Add([int]$record.pid)
    }
  }
  return @($result)
}

function Move-StaleProcessRegistry {
  param(
    [Parameter(Mandatory = $true)][string]$RunFile,
    [Parameter(Mandatory = $true)]$RunPayload
  )

  $liveWorkerPids = @(Get-LiveRecordedWorkerPids -RunPayload $RunPayload)
  $classification = if ($liveWorkerPids.Count -gt 0) { 'orphaned' } else { 'stale' }
  $directory = Split-Path -Parent $RunFile
  $timestamp = Get-Date -Format 'yyyyMMddHHmmssfff'
  $archivePath = Join-Path $directory ("processes.$classification.$timestamp.json")
  Move-Item -LiteralPath $RunFile -Destination $archivePath -Force
  return [pscustomobject]@{
    classification = $classification
    live_worker_pids = @($liveWorkerPids)
    archive_path = $archivePath
  }
}

