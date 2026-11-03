# Stops any running local dev processes of this project (API, worker, portal, demo page),
# whether started from VS Code or a terminal. Only matches this project's commands/ports.
$ErrorActionPreference = "SilentlyContinue"
$stopped = @()

# Anything listening on the API (8000), demo page (8001) or portal dev server (5173) ports.
foreach ($port in 8000, 8001, 5173) {
    Get-NetTCPConnection -LocalPort $port -State Listen | ForEach-Object {
        $process = Get-Process -Id $_.OwningProcess
        if ($process) {
            Stop-Process -Id $process.Id -Force
            $stopped += "$($process.ProcessName) (port $port, pid $($process.Id))"
        }
    }
}

# The worker has no port: find it (and leftover uvicorn/vite processes) by command line.
# Only python/uv/node processes are considered, so shells or editors whose command line merely
# mentions these names are never touched.
$patterns = "*sales_platform.worker*", "*sales_platform.api.app*", "*portal*vite*"
$programs = "python.exe", "pythonw.exe", "uv.exe", "node.exe"
Get-CimInstance Win32_Process | Where-Object {
    $line = $_.CommandLine
    ($programs -contains $_.Name) -and $line -and ($patterns | Where-Object { $line -like $_ })
} | ForEach-Object {
    Stop-Process -Id $_.ProcessId -Force
    $stopped += "$($_.Name) (pid $($_.ProcessId))"
}

if ($stopped.Count) {
    Write-Host "Stopped: $($stopped -join ', ')"
    Start-Sleep -Milliseconds 800  # let the ports close before restarting
} else {
    Write-Host "Nothing was running."
}
exit 0
