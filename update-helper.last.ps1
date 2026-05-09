$ErrorActionPreference = 'Stop'
$script:logPath = 'C:\Users\Administrator\Downloads\视频号批量上传\downloads\update-helper.log'
$oldPid = 6780
$newDir = 'C:\Users\Administrator\Downloads\视频号批量上传\downloads\extracted\windows-v1.2.36\视频号批量上传'
$currentDir = 'C:\Users\Administrator\Downloads\视频号批量上传'
$currentExe = 'C:\Users\Administrator\Downloads\视频号批量上传\视频号批量上传.exe'
$packagePath = 'C:\Users\Administrator\Downloads\视频号批量上传\downloads\sph-app-windows-v1.2.36.zip'
$extractDir = 'C:\Users\Administrator\Downloads\视频号批量上传\downloads\extracted\windows-v1.2.36'
$scriptPath = 'C:\Users\ADMINI~1\AppData\Local\Temp\wx-update-relaunch-nmjky7r1.ps1'
$safeWorkingDir = [System.IO.Path]::GetTempPath()

function Write-UpdateLog {
  param([string]$message)
  try {
    $ts = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    Add-Content -LiteralPath $script:logPath -Value "[$ts] [PowerShell] $message" -Encoding utf8
  } catch {}
}

Write-UpdateLog "helper start: currentDir=$currentDir ; newDir=$newDir"
try {
  Set-Location -LiteralPath $safeWorkingDir
  Write-UpdateLog "switched working dir to: $safeWorkingDir"
} catch {
  Write-UpdateLog ("switch working dir failed: " + $_.Exception.Message)
}

# 等待旧进程完全退出
Write-UpdateLog "waiting for old process ($oldPid) to exit..."
for ($i = 0; $i -lt 60; $i++) {
  if (-not (Get-Process -Id $oldPid -ErrorAction SilentlyContinue)) {
    Write-UpdateLog "old process exited"
    break
  }
  Start-Sleep -Seconds 1
}

# 兜底：如果进程还在，尝试停止它
if (Get-Process -Id $oldPid -ErrorAction SilentlyContinue) {
  Write-UpdateLog "old process still alive, attempting to stop..."
  Stop-Process -Id $oldPid -Force -ErrorAction SilentlyContinue
  Start-Sleep -Seconds 2
}

if (-not (Test-Path -LiteralPath $newDir)) {
  Write-UpdateLog "install source dir missing: $newDir"
  throw "install source dir missing: $newDir"
}

try {
  if ($newDir -ne $currentDir) {
    $installOk = $false
    Write-UpdateLog "starting robocopy update..."
    for ($attempt = 1; $attempt -le 30; $attempt++) {
      # 使用数组拼出原生命令参数，避免中文路径和 /XD /XF 在脚本解析阶段出错
      $robocopyArgs = @($newDir, $currentDir, "/MIR", "/R:0", "/W:0", "/NFL", "/NDL", "/NJH", "/NJS", "/NP")
      $robocopyArgs += '/XD'
      $robocopyArgs += @('C:\Users\Administrator\Downloads\视频号批量上传\uploads', 'C:\Users\Administrator\Downloads\视频号批量上传\screenshots', 'C:\Users\Administrator\Downloads\视频号批量上传\browser-profiles', 'C:\Users\Administrator\Downloads\视频号批量上传\downloads', 'C:\Users\Administrator\Downloads\视频号批量上传\webview-storage', 'C:\Users\Administrator\Downloads\视频号批量上传\data')
      $robocopyArgs += '/XF'
      $robocopyArgs += @('accounts.json', 'accounts.json.bak', 'remembered.json', 'results.csv', 'upload.log', 'app.log', 'last-batch.csv')
      
      & robocopy @robocopyArgs
      $copyCode = $LASTEXITCODE
      Write-UpdateLog "robocopy attempt $attempt exit code: $copyCode"
      if ($copyCode -lt 8) {
        $installOk = $true
        break
      }
      Start-Sleep -Seconds 1
    }
    if (-not $installOk) {
      throw "robocopy failed after retries (exit code $copyCode)"
    }
  }

  $exeName = Split-Path $currentExe -Leaf
  $sourceExe = Join-Path $newDir $exeName
  $installedExe = Join-Path $currentDir $exeName
  Write-UpdateLog "sourceExe=$sourceExe ; installedExe=$installedExe"
  
  if (-not (Test-Path -LiteralPath $sourceExe)) {
    throw "source exe missing: $sourceExe"
  }

  try {
    Write-UpdateLog "ensuring main exe is updated"
    Copy-Item -LiteralPath $sourceExe -Destination $installedExe -Force -ErrorAction Stop
  } catch {
    Write-UpdateLog ("copy main exe failed: " + $_.Exception.Message)
    throw
  }

  $sourceInternal = Join-Path $newDir '_internal'
  $targetInternal = Join-Path $currentDir '_internal'
  if (Test-Path -LiteralPath $sourceInternal) {
    Write-UpdateLog "updating _internal..."
    $internalOk = $false
    for ($attempt = 1; $attempt -le 10; $attempt++) {
      & robocopy $sourceInternal $targetInternal /MIR /R:0 /W:0 /NFL /NDL /NJH /NJS /NP
      $internalCode = $LASTEXITCODE
      Write-UpdateLog "internal robocopy attempt $attempt exit code: $internalCode"
      if ($internalCode -lt 8) { $internalOk = $true; break }
      Start-Sleep -Milliseconds 500
    }
    if (-not $internalOk) {
      throw "copy _internal failed after retries"
    }
  } else {
    Write-UpdateLog "_internal not found in source dir"
  }

  if (-not (Test-Path -LiteralPath $installedExe)) {
    throw "installed exe missing after copy: $installedExe"
  }

  Write-UpdateLog "launching new version: $installedExe"
  $startedProcess = Start-Process -FilePath $installedExe -WorkingDirectory $currentDir -PassThru
  if ($startedProcess -and $startedProcess.Id) {
    Write-UpdateLog ("success, started pid: " + $startedProcess.Id)
  } else {
    Write-UpdateLog "success, process started"
  }
} catch {
  Write-UpdateLog ("CRITICAL ERROR: " + $_.Exception.Message)
  if (Test-Path -LiteralPath $currentExe) {
    Write-UpdateLog "restarting current version as fallback..."
    Start-Process -FilePath $currentExe -WorkingDirectory $currentDir
  }
}

Start-Sleep -Seconds 5
if ($packagePath -and (Test-Path -LiteralPath $packagePath) -and ($packagePath -ne $currentDir)) {
  Remove-Item -LiteralPath $packagePath -Force -ErrorAction SilentlyContinue
}
if ($extractDir -and (Test-Path -LiteralPath $extractDir) -and ($extractDir -ne $currentDir)) {
  Remove-Item -LiteralPath $extractDir -Recurse -Force -ErrorAction SilentlyContinue
}
if (Test-Path -LiteralPath $scriptPath) {
  Remove-Item -LiteralPath $scriptPath -Force -ErrorAction SilentlyContinue
}
