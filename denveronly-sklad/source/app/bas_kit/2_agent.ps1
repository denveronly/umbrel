# Агент «Склади → BAS» для ПК бухгалтерії (Windows PowerShell 5+).
# Раз на 30 с питає «Склади», чи є акти для BAS. Є — завантажує файл завдання і для кожної бази BAS запускає
# у фоні ту саму обробку ЗавантаженняАктівСклади.epf:  1cv8.exe ENTERPRISE /F <база> /N <користувач> /Execute <epf>
# Обробка створює/оновлює НЕПРОВЕДЕНІ «Реалізацію» і «Рахунок» і пише результат, який агент повертає в «Склади».
# Налаштування — agent.json поруч зі скриптом. Журнал — agent.log там само.

$ErrorActionPreference = 'Stop'
$Version = '1.0'
$Dir = $PSScriptRoot
$CfgPath = Join-Path $Dir 'agent.json'
$LogPath = Join-Path $Dir 'agent.log'

function Log($m) {
    $line = (Get-Date -Format 'yyyy-MM-dd HH:mm:ss') + '  ' + $m
    Add-Content -Path $LogPath -Value $line -Encoding UTF8
    if ((Get-Item $LogPath).Length -gt 2MB) { Move-Item $LogPath ($LogPath + '.old') -Force }
}

function Find-1cv8 {
    $roots = @("$env:ProgramFiles", "${env:ProgramFiles(x86)}") | Where-Object { $_ }
    $found = foreach ($r in $roots) {
        foreach ($p in @('1cv8', 'BAS', 'BAS*')) {
            Get-ChildItem -Path (Join-Path $r $p) -Directory -ErrorAction SilentlyContinue |
                ForEach-Object { Get-ChildItem -Path $_.FullName -Filter '1cv8.exe' -Recurse -Depth 3 -ErrorAction SilentlyContinue }
        }
    }
    ($found | Sort-Object { try { [version]($_.VersionInfo.FileVersion -replace '[^\d.]', '') } catch { [version]'0.0' } } -Descending |
        Select-Object -First 1).FullName
}

function Find-Bases {
    # файлові бази зі списку баз 1С поточного користувача Windows
    $f = Join-Path $env:APPDATA '1C\1CEStart\ibases.v8i'
    if (-not (Test-Path $f)) { return @() }
    $name = ''
    $out = @()
    foreach ($l in Get-Content $f -Encoding UTF8) {
        if ($l -match '^\[(.+)\]\s*$') { $name = $Matches[1] }
        elseif ($l -match '^Connect=File="([^"]+)"') { $out += [pscustomobject]@{ name = $name; path = $Matches[1] } }
    }
    $out
}

if (-not (Test-Path $CfgPath)) { throw "Немає $CfgPath — скопіюйте agent.example.json у agent.json і заповніть." }
$cfg = Get-Content $CfgPath -Raw -Encoding UTF8 | ConvertFrom-Json
$url = $cfg.url.TrimEnd('/')
$exe = if ($cfg.v8exe) { $cfg.v8exe } else { Find-1cv8 }
if (-not $exe -or -not (Test-Path $exe)) { throw 'Не знайдено 1cv8.exe — вкажіть шлях у agent.json → "v8exe".' }
$epf = if ($cfg.epf) { $cfg.epf } else { Join-Path $Dir 'ЗавантаженняАктівСклади.epf' }
if (-not (Test-Path $epf)) { throw "Не знайдено обробку $epf" }
$bases = if ($cfg.bases -and $cfg.bases.Count) { $cfg.bases } else { Find-Bases }
if (-not $bases.Count) { throw 'Не знайдено баз BAS — вкажіть їх у agent.json → "bases".' }
$headers = @{ 'X-Token' = $cfg.token; 'X-Agent-Host' = $env:COMPUTERNAME; 'X-Agent-Version' = $Version }
Log "старт: $exe; баз: $($bases.Count) ($(($bases | ForEach-Object { $_.name }) -join ', '))"

while ($true) {
    try {
        $r = Invoke-RestMethod -Uri "$url/bas/agent/jobs" -Headers $headers -TimeoutSec 30
        foreach ($job in $r.jobs) {
            $in = Join-Path $env:TEMP "sklady_job_$($job.id).json"
            Invoke-WebRequest -Uri "$url/bas/agent/job/$($job.id).json" -Headers $headers -OutFile $in -UseBasicParsing -TimeoutSec 60
            Log "завдання $($job.id) ($($job.period))"
            $i = 0
            foreach ($b in $bases) {
                $i++
                $res = Join-Path $env:TEMP "sklady_res_$($job.id)_$i.json"
                Remove-Item $res -ErrorAction SilentlyContinue
                $user = if ($b.user) { $b.user } else { $cfg.user }
                $argv = @('ENTERPRISE', '/F', "`"$($b.path)`"", '/N', "`"$user`"", '/DisableStartupMessages', '/DisableStartupDialogs',
                          '/Execute', "`"$epf`"", '/C', "`"file=$in;result=$res`"")
                if ($b.password) { $argv += @('/P', "`"$($b.password)`"") }
                $p = Start-Process -FilePath $exe -ArgumentList $argv -PassThru -WindowStyle Hidden
                if (-not $p.WaitForExit(600000)) { $p.Kill(); Log "  $($b.name): перевищено 10 хв — сеанс зупинено" }
                $body = if (Test-Path $res) { [IO.File]::ReadAllBytes($res) } else { [Text.Encoding]::UTF8.GetBytes('[]') }
                $last = if ($i -eq $bases.Count) { '1' } else { '0' }
                $q = "job=$($job.id)&base=" + [uri]::EscapeDataString($b.name) + "&last=$last"
                Invoke-RestMethod -Uri "$url/bas/agent/result?$q" -Method Post -Headers $headers -Body $body `
                    -ContentType 'application/json; charset=utf-8' -TimeoutSec 60 | Out-Null
                Log ("  " + $b.name + ": " + $(if (Test-Path $res) { 'готово' } else { 'без результату (див. BAS)' }))
                Remove-Item $res -ErrorAction SilentlyContinue
            }
            Remove-Item $in -ErrorAction SilentlyContinue
        }
    } catch {
        Log "помилка: $($_.Exception.Message)"
    }
    Start-Sleep -Seconds 30
}
