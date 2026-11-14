@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
echo ============================================================
echo  发布到 GitHub：wvwvwvwvw/photo-dedupe
echo ------------------------------------------------------------
echo  前提：已经在 GitHub 网页上点过「Create repository」建好空仓库
echo  运行后如果弹出浏览器登录窗口，点一下授权即可
echo ============================================================
echo.
git remote get-url origin >nul 2>&1
if errorlevel 1 git remote add origin https://github.com/wvwvwvwvw/photo-dedupe.git
echo 正在推送...
git push -u origin main
if errorlevel 1 (
  echo.
  echo 推送失败。常见原因：
  echo   1）仓库还没建（先到 https://github.com/new 用 photo-dedupe 建一个空的，不要勾 README）
  echo   2）没登录：弹出的浏览器窗口里点「Authorize」授权
  echo   3）仓库名或用户名和上面的不一致
  pause
  exit /b 1
)
echo.
echo 发布成功：https://github.com/wvwvwvwvw/photo-dedupe
pause
