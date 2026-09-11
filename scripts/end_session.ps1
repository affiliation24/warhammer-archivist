<#
.SYNOPSIS
    Запускать перед тем, как вынимать флешку из чужого/временного ноута.

.DESCRIPTION
    Делает три вещи:
      1. Останавливает процессы python.exe, у которых открыты файлы на флешке —
         без этого Windows может отказаться "безопасно извлечь" диск, а
         принудительное выдёргивание при открытых хендлах рискует повредить
         данные на самой флешке (ChromaDB особенно чувствительна к этому).
      2. Проверяет дефолтные пути кэша на хост-машине (кэш pip, кэш моделей
         Hugging Face) и убирает оттуда всё, что относится к этому проекту —
         на случай, если PIP_CACHE_DIR/HF_HOME почему-то не сработали и что-то
         всё же скачалось не на флешку, а в профиль пользователя Windows.
         НЕ трогает ничего, кроме файлов, которые могли появиться от запуска
         этого проекта — не системные логи, не реестр, не история ОС.
      3. Безопасно отключает том флешки штатным средством Windows.

.PARAMETER Drive
    Буква диска флешки, например "E:\"

.EXAMPLE
    .\end_session.ps1 -Drive E:\
#>
param(
    [Parameter(Mandatory = $true)]
    [string]$Drive
)

$DriveLetter = $Drive.TrimEnd('\', '/').TrimEnd(':')

Write-Host "==> Останавливаю процессы, держащие файлы на диске $Drive"
Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" | ForEach-Object {
    if ($_.ExecutablePath -and $_.ExecutablePath.StartsWith("${DriveLetter}:", [System.StringComparison]::OrdinalIgnoreCase)) {
        Write-Host "    останавливаю PID $($_.ProcessId) ($($_.ExecutablePath))"
        Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
    }
}
Start-Sleep -Seconds 1

Write-Host "==> Проверяю, не осел ли кэш на хост-машине"
$leaked = $false

$hostPipCache = Join-Path $env:LOCALAPPDATA "pip\Cache"
if (Test-Path $hostPipCache) {
    $recent = Get-ChildItem $hostPipCache -Recurse -ErrorAction SilentlyContinue |
        Where-Object { $_.LastWriteTime -gt (Get-Date).AddHours(-6) }
    if ($recent) {
        Write-Host "    найден свежий кэш pip в профиле пользователя — удаляю ($($recent.Count) файлов)"
        Remove-Item $hostPipCache -Recurse -Force -ErrorAction SilentlyContinue
        $leaked = $true
    }
}

$hostHfCache = Join-Path $env:USERPROFILE ".cache\huggingface\hub"
foreach ($model in @("models--intfloat--multilingual-e5-large", "models--BAAI--bge-reranker-v2-m3")) {
    $path = Join-Path $hostHfCache $model
    if (Test-Path $path) {
        Write-Host "    найдена модель '$model' в профиле пользователя (HF_HOME не сработал?) — удаляю"
        Remove-Item $path -Recurse -Force -ErrorAction SilentlyContinue
        $leaked = $true
    }
}

if (-not $leaked) {
    Write-Host "    ничего не найдено — HF_HOME/PIP_CACHE_DIR сработали как надо"
}

Write-Host "==> Безопасно отключаю $Drive"
try {
    $shell = New-Object -ComObject Shell.Application
    $shell.NameSpace(17).ParseName($Drive).InvokeVerb("Eject")
    Write-Host "    диск отключён, флешку можно вынимать"
} catch {
    Write-Warning "Не удалось отключить автоматически — извлеки через 'Безопасное извлечение устройств' в трее."
}
