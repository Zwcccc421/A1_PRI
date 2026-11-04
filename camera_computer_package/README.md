# 摄像头电脑端采集服务

本文件夹运行在连接 USB 摄像头和麦克风的电脑上。它会打开摄像头、采集麦克风，并提供 HTTP 接口给 A1 服务器读取。

## 1. 安装依赖

```bash
python -m pip install -r requirements-camera.txt
```

Linux 如果安装 `sounddevice` 后无法打开麦克风，补装系统音频库：

```bash
sudo apt-get install libportaudio2
```

## 2. 启动

Linux/macOS：

```bash
./run_camera_stream.sh
```

Windows：

```bat
run_camera_stream.bat
```

默认使用摄像头编号 `0` 和系统默认麦克风，监听 `0.0.0.0:18081`。

如果摄像头不是编号 0：

```bash
python local_usb_camera_stream.py --index 1 --host 0.0.0.0 --port 18081 --enable-audio
```

如果需要指定麦克风：

```bash
python local_usb_camera_stream.py --host 0.0.0.0 --port 18081 --enable-audio --audio-device 1
```

## 3. A1 服务器使用的地址

把 `<摄像头电脑内网IP>` 替换成运行本服务电脑的内网 IP：

```text
http://<摄像头电脑内网IP>:18081/latest.jpg
http://<摄像头电脑内网IP>:18081/audio.pcm
```

例如：

```text
http://10.113.46.235:18081/latest.jpg
http://10.113.46.235:18081/audio.pcm
```

先在 A1 服务器测试：

```bash
curl http://<摄像头电脑内网IP>:18081/health
curl -o test.jpg http://<摄像头电脑内网IP>:18081/latest.jpg
```

看到 `test.jpg` 成功生成后，再运行 A1 的 `run_streaming_a1_full_20260624.sh`。

注意：摄像头电脑和 A1 服务器必须网络互通，防火墙需要允许 TCP `18081` 端口。
