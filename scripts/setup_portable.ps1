<#
.SYNOPSIS
    Разово готовит venv под Windows на уже подготовленной флешке (код + данные
    + опционально кэш моделей должны там уже лежать — например, скопированы
    туда через scripts/setup_portable.sh с Mac).

.DESCRIPTION
    В отличие от Mac-версии, venv здесь называется .venv-win (не .venv) —
    чтобы обе ОС могли жить на одной флешке без конфликта. Кэш pip
    перенаправляется на саму флешку (PIP_CACHE_DIR), а не в профиль
    пользователя Windows — иначе после установки на диске хост-машины
    останутся скачанные .whl-файлы даже после того, как флешку вынули.

.PARAMETER Drive
    Буква диска флешки, например "E:\"

.EXAMPLE
    .\setup_portable.ps1 -Drive E:\
#>
param(
    [Parameter(Mandatory = $true)]
    [string]$Drive
)

$ErrorActionPreference = "Stop"

$Target = Join-Path $Drive "warhammer-archivist"

if (-not (Test-Path $Target)) {
    Write-Error "Не найдено '$Target' — сначала скопируй код и данные на флешку (см. README)."
    exit 1
}
if (-not (Test-Path (Join-Path $Target "src"))) {
    Write-Error "'$Target\src' не найден — похоже, на флешке нет кода проекта."
    exit 1
}

$PipCacheDir = Join-Path $Target ".pip_cache"
New-Item -ItemType Directory -Force -Path $PipCacheDir | Out-Null
$env:PIP_CACHE_DIR = $PipCacheDir

$VenvPath = Join-Path $Target ".venv-win"

Write-Host "==> Создаю venv ($VenvPath)"
python -m venv $VenvPath

$Pip = Join-Path $VenvPath "Scripts\pip.exe"
$PyExe = Join-Path $VenvPath "Scripts\python.exe"

Write-Host "==> Ставлю CPU-only torch (без CUDA — на ноуте без Nvidia GPU обычная сборка torch с PyPI весит в разы больше без всякой пользы)"
& $Pip install --quiet torch==2.14.0 --index-url https://download.pytorch.org/whl/cpu

Write-Host "==> Ставлю остальные зависимости"
& $Pip install --quiet -r (Join-Path $Target "requirements.txt")

# Опционально: если на этой Windows-машине уже когда-то скачивались нужные
# модели (например, для другого проекта) — переиспользуем, чтобы не качать
# заново. Если их там нет — не страшно, retriever.py сам скачает при первом
# запуске прямо в hf_cache на флешке (HF_HOME настроен в коде проекта).
$HostHfCache = Join-Path $env:USERPROFILE ".cache\huggingface\hub"
$FlashHfCache = Join-Path $Target "hf_cache\hub"
New-Item -ItemType Directory -Force -Path $FlashHfCache | Out-Null
foreach ($model in @("models--intfloat--multilingual-e5-large", "models--BAAI--bge-reranker-v2-m3")) {
    $src = Join-Path $HostHfCache $model
    $dst = Join-Path $FlashHfCache $model
    if ((Test-Path $src) -and (-not (Test-Path $dst))) {
        Write-Host "==> Нашёл $model в кэше этой машины — копирую на флешку, чтобы не качать заново"
        Copy-Item -Recurse $src $dst
    }
}

Write-Host ""
Write-Host "==> Готово: $VenvPath"
Write-Host "    Запуск: cd '$Target\src'; & '$PyExe' chat.py"
Write-Host ""
Write-Host "    Перед тем как вынимать флешку — запусти scripts\end_session.ps1 -Drive $Drive"
