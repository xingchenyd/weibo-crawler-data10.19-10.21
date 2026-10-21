@echo off
rem 一键熄屏：向系统发送 SC_MONITORPOWER 关闭显示器。
rem 爬虫照常运行（系统不睡眠），鼠标移动或按任意键即恢复显示。
.venv\Scripts\python.exe -c "import ctypes; ctypes.windll.user32.SendMessageW(0xFFFF, 0x0112, 0x0170, 2)"
exit
