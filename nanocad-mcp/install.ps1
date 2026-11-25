#Requires -Version 5.1
param([ValidateSet('Auto','Codex','Claude')][string]$Client = 'Auto')
$ErrorActionPreference = 'Stop'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
. (Join-Path $PSScriptRoot 'codex-mcp.ps1')

$clients = Get-CadClients $Client -AllowManual
if (Get-Process -Name 'ncad*','nmc*' -ErrorAction SilentlyContinue) {
    throw 'Закройте nanoCAD и повторите установку.'
}
$dest = Join-Path $env:LOCALAPPDATA 'nanoCAD-MCP'
$python = Join-Path $dest '.venv\Scripts\python.exe'
$server = Join-Path $dest 'nanocad_mcp.py'
$files = 'nanocad_mcp.py','cad_access.py','cad_toggle.lsp','control.ps1','NanoCAD-MCP.cfg','NanoCAD-MCP.cuix','NanoCAD-MCP.ico','NanoCadMcp.Tools26.dll'
foreach ($file in $files) {
    if (!(Test-Path -LiteralPath (Join-Path $PSScriptRoot $file) -PathType Leaf)) { throw "В комплекте отсутствует $file." }
}

function Get-SetupUv {
    $command = Get-Command uv.exe -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($command) { return $command.Source }
    $userUv = Join-Path $env:USERPROFILE '.local\bin\uv.exe'
    if (Test-Path -LiteralPath $userUv -PathType Leaf) { return $userUv }

    $toolsDir = Join-Path $dest 'tools'
    $localUv = Join-Path $toolsDir 'uv.exe'
    if (Test-Path -LiteralPath $localUv -PathType Leaf) { return $localUv }
    New-Item -ItemType Directory -Path $toolsDir -Force | Out-Null
    $archive = Join-Path $toolsDir ("uv-{0}.zip" -f [guid]::NewGuid())
    $partial = "$archive.exe"
    try {
        Write-Host 'Скачиваем uv для Windows x64...'
        Invoke-WebRequest 'https://github.com/astral-sh/uv/releases/download/0.8.22/uv-x86_64-pc-windows-msvc.zip' -UseBasicParsing -OutFile $archive
        if ((Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash -ne '5049375aa2a5162f132b2c1cb992e25d42d47d934cab8c174dbe6f60973dcc12') {
            throw 'Контрольная сумма архива uv не совпала. Повторите установку.'
        }
        Add-Type -AssemblyName System.IO.Compression.FileSystem
        $zip = [IO.Compression.ZipFile]::OpenRead($archive)
        try {
            $entry = $zip.Entries | Where-Object Name -eq 'uv.exe' | Select-Object -First 1
            if (!$entry) { throw 'В архиве uv отсутствует uv.exe.' }
            [IO.Compression.ZipFileExtensions]::ExtractToFile($entry, $partial)
        } finally { $zip.Dispose() }
        Move-Item -LiteralPath $partial -Destination $localUv -Force
    } finally {
        foreach ($file in $partial,$archive) {
            if (Test-Path -LiteralPath $file) { Remove-Item -LiteralPath $file -Force }
        }
    }
    return $localUv
}

Write-Host 'Устанавливаем nanoCAD MCP для текущего пользователя Windows...'
New-Item -ItemType Directory -Path $dest -Force | Out-Null
$uv = Get-SetupUv
if (!(Test-Path -LiteralPath $python)) {
    & $uv venv (Join-Path $dest '.venv') --python 3.12
    if ($LASTEXITCODE -ne 0) { throw 'Не удалось подготовить Python 3.12.' }
}
if (!(Test-Path -LiteralPath (Join-Path $dest '.venv\Scripts\pythonw.exe'))) { throw 'В окружении Python отсутствует pythonw.exe.' }
& $uv pip install --python $python 'mcp>=1.10,<2' pywin32
if ($LASTEXITCODE -ne 0) { throw 'Не удалось установить библиотеки Python.' }
foreach ($file in $files) { Copy-Item -LiteralPath (Join-Path $PSScriptRoot $file) -Destination $dest -Force }

$mode = if ($clients.Claude) { '--install' } else { '--install-ui' }
& $python $server $mode
if ($LASTEXITCODE -ne 0) { throw 'Не удалось настроить nanoCAD MCP. См. сообщение выше.' }
& (Join-Path $dest 'control.ps1') -InstallShortcut
if ($clients.Codex) {
    Register-CadCodexServer -Codex $clients.Codex -Name 'nanocad-mcp' -Server $python -ServerArgs @($server)
}

Write-Host 'Готово. Откройте чертёж и включите доступ на вкладке MCP в nanoCAD.' -ForegroundColor Green
if ($clients.Codex) {
    Write-Host 'Перезапустите Codex, войдите в свой аккаунт ChatGPT и откройте новый чат.'
    Write-Host 'Проверка: «Вызови status сервера nanocad-mcp и скажи, какой чертёж открыт».'
}
if ($clients.Claude) { Write-Host 'Для работы в Claude Desktop запустите его снова.' }
if (!$clients.Codex -and !$clients.Claude) {
    Write-Host 'Установлен сервер nanoCAD. Для автоматической регистрации установите и откройте Codex, затем повторите install.bat. Для другого клиента параметры подключения доступны по ярлыку «nanoCAD MCP».'
}
