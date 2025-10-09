pyinstaller --name "yuketang_monitor" --add-data "config.ini;." --add-data "courses;courses" --hidden-import "PyQt6.sip" --hidden-import "json" --hidden-import "os" --hidden-import "re" --hidden-import "time" --hidden-import "datetime" --hidden-import "threading" --onefile --windowed main_pyqt6_yuketang_monitor.py
pause
