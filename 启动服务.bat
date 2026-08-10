@echo off
setlocal
chcp 65001 >nul

set "REPO_ROOT=%~dp0"
set "CONFIG_PATH=%REPO_ROOT%config\app.example.yaml"
set "PYTHON_PATH=%REPO_ROOT%.venv-control\Scripts\python.exe"
set "HEALTH_URL=http://127.0.0.1:8765/api/v1/health"
set "WEBUI_URL=http://127.0.0.1:8765/"

pushd "%REPO_ROOT%" >nul 2>&1
if errorlevel 1 (
  echo [错误] 无法进入项目目录：%REPO_ROOT%
  if /I not "%VOICE_PIPELINE_NO_PAUSE%"=="1" pause
  exit /b 1
)

where pwsh >nul 2>&1
if errorlevel 1 (
  set "ERROR_MESSAGE=未找到 PowerShell 7（pwsh）。请先安装 PowerShell 7。"
  goto failed
)

if not exist "%CONFIG_PATH%" (
  set "ERROR_MESSAGE=缺少启动配置 config\app.example.yaml。"
  goto failed
)

if not exist "%PYTHON_PATH%" (
  set "ERROR_MESSAGE=缺少控制环境 .venv-control\Scripts\python.exe，请先运行 scripts\setup-control.ps1。"
  goto failed
)

pwsh -NoProfile -Command "$ErrorActionPreference='SilentlyContinue'; try { $health=Invoke-RestMethod -Uri '%HEALTH_URL%' -TimeoutSec 2; if ($health.status -eq 'ready') { exit 0 } } catch {}; exit 1" >nul 2>&1
if errorlevel 1 (
  echo 正在启动控制服务、IndexTTS2 和 GPT-SoVITS，请稍候...
  pwsh -NoProfile -ExecutionPolicy Bypass -File "%REPO_ROOT%scripts\start.ps1" -Config "%CONFIG_PATH%" -PythonExecutable "%PYTHON_PATH%" -Json
  if errorlevel 1 (
    set "ERROR_MESSAGE=服务启动失败，请查看 runtime\logs\control.stderr.log。"
    goto failed
  )
) else (
  echo 服务已经运行，正在打开 WebUI...
)

if /I not "%VOICE_PIPELINE_NO_BROWSER%"=="1" start "" "http://127.0.0.1:8765/"
popd >nul
exit /b 0

:failed
echo.
echo [错误] %ERROR_MESSAGE%
if /I not "%VOICE_PIPELINE_NO_PAUSE%"=="1" pause
popd >nul
exit /b 1
