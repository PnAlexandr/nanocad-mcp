#Requires -Version 5.1
# Входит в оба комплекта, чтобы каждый установщик можно было раздавать отдельно.
function Find-CadCodex {
    $command = Get-Command codex.exe, codex.cmd -CommandType Application -ErrorAction SilentlyContinue |
        Select-Object -First 1
    if ($command) { return $command.Source }

    $bin = Join-Path $env:LOCALAPPDATA 'OpenAI\Codex\bin'
    $exe = Get-ChildItem -Path "$bin\*\codex.exe" -File -ErrorAction SilentlyContinue |
        Sort-Object LastWriteTime -Descending | Select-Object -First 1
    if ($exe) { return $exe.FullName }

    if (!(Get-Command Get-AppxPackage -ErrorAction SilentlyContinue)) { return }
    foreach ($package in (Get-AppxPackage -Name 'OpenAI.*' -ErrorAction SilentlyContinue)) {
        $exe = Join-Path $package.InstallLocation 'app\resources\codex.exe'
        if (Test-Path -LiteralPath $exe -PathType Leaf) { return $exe }
    }
}

function Get-CadClients([string]$Client, [switch]$AllowManual) {
    $codex = if ($Client -ne 'Claude') { Find-CadCodex }
    $claude = $false
    if ($Client -ne 'Codex') {
        $claude = (Test-Path -LiteralPath (Join-Path $env:APPDATA 'Claude')) -or
            (Test-Path -Path (Join-Path $env:LOCALAPPDATA 'Packages\*Claude*\LocalCache\Roaming\Claude'))
    }
    if ($Client -eq 'Claude' -and !$claude) { throw 'Сначала запустите Claude Desktop под своей учётной записью Windows.' }
    if (!$codex -and !$claude -and !($AllowManual -and $Client -eq 'Auto')) {
        throw 'Установите и откройте Codex для Windows под своим пользователем, затем повторите установку. Также поддерживается Codex CLI в PATH.'
    }
    if ($claude -and (Get-Process -Name Claude -ErrorAction SilentlyContinue)) {
        if (!$codex) { throw 'Полностью закройте Claude Desktop через значок в трее и повторите установку.' }
        Write-Host 'Claude Desktop открыт. Настраиваем только Codex.'
        $claude = $false
    }
    if ($claude -and !$codex -and $Client -eq 'Auto') {
        Write-Host 'Найден только Claude Desktop. Для подключения Codex установите его и повторно запустите этот установщик.'
    }
    [pscustomobject]@{ Codex = $codex; Claude = $claude }
}

function Register-CadCodexServer {
    param([string]$Codex, [string]$Name, [string]$Server, [string[]]$ServerArgs = @())
    foreach ($file in @($Server) + $ServerArgs) {
        if (!(Test-Path -LiteralPath $file -PathType Leaf)) { throw "Не найден файл сервера $Name ($file)." }
    }

    # Исключаем проектные настройки из проверки. CLI сам учитывает CODEX_HOME.
    Push-Location -LiteralPath $env:LOCALAPPDATA
    $encoding = [Console]::OutputEncoding
    try {
        [Console]::OutputEncoding = New-Object Text.UTF8Encoding($false)
        # Оба установщика могут одновременно добавлять записи в один профиль.
        $identity = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
        $mutex = [Threading.Mutex]::new($false, "Local\CadCodexRegistration-$identity")
        $locked = $false
        try { $locked = $mutex.WaitOne(30000) } catch [Threading.AbandonedMutexException] { $locked = $true }
        if (!$locked) { throw 'Codex настраивается другим установщиком. Повторите установку после его завершения.' }
        $entries = & $Codex mcp list --json
        if ($LASTEXITCODE -ne 0) { throw 'Codex не смог прочитать настройки MCP. Проверьте сообщение выше.' }
        $existing = ($entries -join [Environment]::NewLine | ConvertFrom-Json) |
            Where-Object { $_.name -ceq $Name } | Select-Object -First 1
        if (!$existing) {
            & $Codex mcp add $Name -- $Server @ServerArgs
            if ($LASTEXITCODE -ne 0) { throw "Codex не смог добавить $Name." }
        }
        $json = & $Codex mcp get $Name --json
        if ($LASTEXITCODE -ne 0) { throw "Codex не смог прочитать запись $Name." }
        $entry = $json -join [Environment]::NewLine | ConvertFrom-Json
        $transport = $entry.transport
        $argumentsMatch = @($transport.args).Count -eq $ServerArgs.Count
        for ($i = 0; $argumentsMatch -and $i -lt $ServerArgs.Count; $i++) {
            $argumentsMatch = $transport.args[$i] -eq $ServerArgs[$i]
        }
        if ($transport.type -ne 'stdio' -or $transport.command -ne $Server -or !$argumentsMatch) {
            throw "В Codex уже есть другое подключение с именем $Name. Проверьте его в настройках MCP. Ожидаемая команда: $Server $($ServerArgs -join ' ')"
        }
        if (!$entry.enabled) { throw "В настройках MCP Codex включите существующий сервер $Name." }
        Write-Host "Codex подтвердил регистрацию сервера $Name."
    } finally {
        if ($locked) { $mutex.ReleaseMutex() }
        if ($mutex) { $mutex.Dispose() }
        [Console]::OutputEncoding = $encoding
        Pop-Location
    }
}
