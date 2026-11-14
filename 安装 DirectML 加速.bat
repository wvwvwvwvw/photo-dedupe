@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
echo ============================================================
echo  安装 DirectML 加速（给非 NVIDIA 显卡用）
echo ------------------------------------------------------------
echo  说明：
echo    * 这个方案会把 PyTorch 换成 CPU 版 + torch-directml
echo    * 换完之后 NVIDIA 的 CUDA 加速就没了（想还原：重跑一次 pip 装 cu130 版）
echo    * 只影响图片查重的 GPU 路径，其他功能不受影响
echo ============================================================
echo.
set /p OK=确认继续吗？(y/N)
if /i not "%OK%"=="y" goto :eof
echo.
echo 正在安装 torch-directml（约 200-400 MB）...
runtime\python.exe -m pip install --upgrade torch-directml
if errorlevel 1 goto :fail
echo.
echo 自检结果：
runtime\python.exe app.py --selfcheck
echo.
pause
goto :eof

:fail
echo.
echo 安装失败。可以手动执行：
echo   runtime\python.exe -m pip install torch-directml
pause
