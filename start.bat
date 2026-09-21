@echo off
rem ==========================================================================
rem  本文件有两处字符陷阱，都已修掉，改动时注意别退回去：
rem   1) 换行必须是 CRLF，不能用裸 LF
rem   2) 端口检测必须用 findstr /C: 做字面量匹配
rem  另外保留下面的 chcp 65001：控制台默认代码页是 936，不切换中文会显示成乱码。
rem
rem  为什么 1)：cmd 按字节偏移逐行读批处理，chcp 切到 UTF-8 后偏移计算与裸 LF 对不上，
rem  会逐行错位 —— 实测把 echo 啃成 cho、中文碎片被当命令执行，报出
rem  「'了，直接打开：' 不是内部或外部命令」这种错。改成 CRLF 后错位消失。
rem  注意是**两个条件同时存在**才触发：单独换 LF、或单独去掉 chcp，都不会出问题。
rem
rem  编码保持现状（UTF-8 不带 BOM）即可。实测本机 cmd 带 BOM 也能正常跑，
rem  但 BOM 对 .bat 没有用途，不必添加。
rem ==========================================================================
chcp 65001 >nul
cd /d "%~dp0backend"

rem  检测 8000 是否已在监听。**必须用 /C: 做字面量匹配**：
rem 写成 findstr ":8000 " 时，findstr 会把模式里的空格当作多个搜索词的**分隔符**，
rem 空格被吃掉后模式退化成 ":8000"，于是会匹配上远端 IPv6 地址里恰好含 ":8000:" 的连接
rem ——实测端口空着时也能匹配到 6 行、errorlevel=0，结果永远误判成「服务已经在跑了」，
rem 服务根本起不来。再串一道 LISTENING，避免把「连到别人 8000 端口的客户端连接」当成自己在监听。
netstat -ano | findstr /C:":8000 " | findstr /C:"LISTENING" >nul
if %errorlevel%==0 (
  echo.
  echo  服务已经在跑了，直接打开： http://127.0.0.1:8000
  echo  （手机看的话用启动时打印的那个局域网地址）
  echo.
  pause
  exit /b 0
)

echo.
echo  正在启动复盘站点，按 Ctrl+C 可以停掉，关掉这个窗口也会停。
echo.
..\.venv\Scripts\python.exe -m app.main
echo.
echo  服务已停止。
pause
