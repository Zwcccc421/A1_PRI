@echo off
cd /d "%~dp0"
python local_usb_camera_stream.py --index 0 --host 0.0.0.0 --port 18081 --enable-audio --sample-rate 16000 --channels 1 --audio-seconds 5
pause
