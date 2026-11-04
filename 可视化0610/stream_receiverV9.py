import asyncio
import httpx
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Header
from fastapi.responses import StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional, List
from pathlib import Path
from datetime import datetime
import json
import subprocess
import sys

# ====================== 应用初始化 ======================
app = FastAPI(title="智能养老机器人-全自动中央调度后端")

# 全局跨域配置（允许前端浏览器、局域网设备访问）
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 密钥配置（所有WebSocket/API鉴权统一使用）
API_KEY = "my-secret-key"

# ====================== 摄像头地址配置 ======================
# 老人第三视角HTTP拉流地址
ELDER_CAMERA_URL = "http://219.223.204.104:18080/latest.jpg"
# 拉流间隔（秒，0.05 = 20帧/秒，可根据网络情况调整）
ELDER_CAMERA_INTERVAL = 0.05

# 机器人主视角HTTP拉流地址（MJPEG流）
ROBOT_CAMERA_URL = "http://10.113.53.104:18080/video.mjpg"
# 抓取视角HTTP拉流地址（MJPEG流）
GRASP_CAMERA_URL = "http://10.113.53.104:18082/color.mjpg"
# MJPEG流异常重连间隔（秒）
MJPEG_RECONNECT_INTERVAL = 1.0

# 日志文件路径配置
BASE_DIR = Path(__file__).resolve().parent
SAVE_DIR = BASE_DIR / "received_data"
SAVE_DIR.mkdir(parents=True, exist_ok=True)
TEXT_LOG_PATH = SAVE_DIR / "texts.jsonl"
SIGNAL_LOG_PATH = SAVE_DIR / "signals.jsonl"
LOCAL_CAMERA_HEALTH_URL = "http://127.0.0.1:18081/health"
LOCAL_CAMERA_SCRIPT = BASE_DIR.parent / "camera_computer_package" / "local_usb_camera_stream.py"
# Current Windows/OpenCV enumeration: 0 = external 720P USB Camera.
# 当前现场确认：1 为电脑内置摄像头，老人视角使用外接 USB 摄像头 0。
LOCAL_CAMERA_INDEX = 0
# The USB camera audio endpoint is effectively silent. Device 2 is the
# microphone-array input that continuously produces frames for streaming ASR.
LOCAL_AUDIO_DEVICE = 2
LOCAL_CAMERA_PROCESS = None

print(f"[启动] 文本日志路径: {TEXT_LOG_PATH.resolve()}")
print(f"[启动] 信号日志路径: {SIGNAL_LOG_PATH.resolve()}")

# ====================== 数据模型定义 ======================
class TextPayload(BaseModel):
    """接收外部文本JSON数据模型"""
    text: str
    source: Optional[str] = None
    meta: Optional[dict] = None

class ServerSignalPayload(BaseModel):
    """接收业务信号数据模型"""
    event: str
    text: Optional[str] = None
    value: Optional[int] = None  # 0=老人视角 1=机器人视角
    task_type: Optional[str] = None
    task_id: Optional[str] = None

# ====================== 全局内存缓冲区 & 连接池 ======================
# 三路视频帧缓冲区（独立存储，互不干扰）
latest_frames = {
    "elder": None,   # 老人第三视角（HTTP单帧轮询拉取）
    "robot": None,   # 机器人主视角（HTTP MJPEG拉取，兼容原WebSocket推流）
    "grasp": None,   # 抓取视角（HTTP MJPEG拉取）
}

# 大屏前端 WebSocket 连接列表（用于信号广播）
connected_dashboards: List[WebSocket] = []

# ====================== 本机音视频采集服务 ======================
async def local_camera_is_ready() -> bool:
    """Return whether the local camera/audio service is already healthy."""
    try:
        async with httpx.AsyncClient(timeout=1.0) as client:
            response = await client.get(LOCAL_CAMERA_HEALTH_URL)
        return response.status_code == 200
    except httpx.HTTPError:
        return False


async def ensure_local_camera_service() -> None:
    """Start the local USB camera/audio service only when it is not running."""
    global LOCAL_CAMERA_PROCESS
    if await local_camera_is_ready():
        print("[启动] 本机音视频采集服务已运行")
        return
    if not LOCAL_CAMERA_SCRIPT.is_file():
        print(f"[启动] 未找到本机音视频采集脚本: {LOCAL_CAMERA_SCRIPT}")
        return

    LOCAL_CAMERA_PROCESS = subprocess.Popen(
        [
            sys.executable,
            str(LOCAL_CAMERA_SCRIPT),
            "--index", str(LOCAL_CAMERA_INDEX),
            "--host", "127.0.0.1",
            "--port", "18081",
            "--enable-audio",
            "--audio-device", str(LOCAL_AUDIO_DEVICE),
        ],
        cwd=str(LOCAL_CAMERA_SCRIPT.parent),
    )
    for _ in range(10):
        await asyncio.sleep(0.5)
        if await local_camera_is_ready():
            print("[启动] 本机音视频采集服务已启动")
            return
    print("[启动] 本机音视频采集服务启动超时，请检查摄像头和麦克风")


# ====================== 后台拉流任务 ======================
async def pull_elder_camera_task():
    """
    后台常驻异步任务：持续拉取老人视角HTTP图片
    加入JPEG合法性校验，匹配调试脚本的帧头帧尾判断逻辑
    """
    async with httpx.AsyncClient(timeout=3.0) as client:
        while True:
            try:
                response = await client.get(ELDER_CAMERA_URL)
                if response.status_code == 200:
                    frame_data = response.content
                    # JPEG合法性校验：必须以FFD8开头、FFD9结尾，且长度大于100字节
                    if (len(frame_data) > 100 
                        and frame_data.startswith(b"\xff\xd8") 
                        and frame_data.endswith(b"\xff\xd9")):
                        latest_frames["elder"] = frame_data
            except Exception as e:
                print(f"[老人摄像头拉取异常] {str(e)}")
            # 控制拉取帧率
            await asyncio.sleep(ELDER_CAMERA_INTERVAL)

async def pull_mjpeg_camera_task(camera_id: str, url: str):
    """
    后台常驻异步任务：拉取MJPEG格式视频流，解析每一帧存入全局缓冲区
    校验规则与elder视角完全一致：JPEG帧头帧尾、最小长度校验
    """
    boundary_marker = b'--frame'
    async with httpx.AsyncClient(timeout=10.0) as client:
        while True:
            try:
                # 流式请求MJPEG流，持续接收数据
                async with client.stream("GET", url) as response:
                    if response.status_code != 200:
                        print(f"[{camera_id}摄像头] 请求失败，状态码: {response.status_code}")
                        await asyncio.sleep(MJPEG_RECONNECT_INTERVAL)
                        continue
                    
                    buffer = b''
                    # 持续读取流数据并解析帧
                    async for chunk in response.aiter_bytes(chunk_size=4096):
                        buffer += chunk
                        # 循环解析缓冲区中的所有完整帧
                        while boundary_marker in buffer:
                            # 找到MJPEG帧的HTTP头结束位置
                            header_end = buffer.find(b'\r\n\r\n')
                            if header_end == -1:
                                break
                            # 找到下一帧边界，截取当前帧的JPEG数据
                            frame_start = header_end + 4
                            frame_end = buffer.find(boundary_marker, frame_start)
                            if frame_end == -1:
                                break
                            
                            # 去除帧尾的换行符，得到纯净JPEG数据
                            frame_data = buffer[frame_start:frame_end].rstrip(b'\r\n')
                            
                            # 与elder视角完全一致的JPEG合法性校验
                            if (len(frame_data) > 100 
                                and frame_data.startswith(b"\xff\xd8") 
                                and frame_data.endswith(b"\xff\xd9")):
                                latest_frames[camera_id] = frame_data
                            
                            # 移除已处理的缓冲区数据，避免内存膨胀
                            buffer = buffer[frame_end:]
            except Exception as e:
                print(f"[{camera_id}摄像头拉取异常] {str(e)}")
                # 异常后等待指定时间再重连
                await asyncio.sleep(MJPEG_RECONNECT_INTERVAL)

# 服务启动时自动运行所有后台拉流任务
@app.on_event("startup")
async def startup_event():
    await ensure_local_camera_service()
    asyncio.create_task(pull_elder_camera_task())
    asyncio.create_task(pull_mjpeg_camera_task("robot", ROBOT_CAMERA_URL))
    asyncio.create_task(pull_mjpeg_camera_task("grasp", GRASP_CAMERA_URL))
    print("[启动] 老人视角HTTP拉流后台任务已启动")
    print("[启动] 机器人视角MJPEG拉流后台任务已启动")
    print("[启动] 抓取视角MJPEG拉流后台任务已启动")


@app.on_event("shutdown")
async def shutdown_event():
    if LOCAL_CAMERA_PROCESS and LOCAL_CAMERA_PROCESS.poll() is None:
        LOCAL_CAMERA_PROCESS.terminate()
        print("[停止] 已停止由 A4 启动的本机音视频采集服务")

# ====================== 接口1：接收外部文本数据 ======================
@app.post("/api/text")
async def receive_text(
    payload: TextPayload,
    x_api_key: Optional[str] = Header(None, alias="X-API-Key")
):
    # 鉴权校验
    if x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid API key")

    print(f"[接收文本] 来源: {payload.source} | 附加信息: {payload.meta}")
    print(f"[接收文本内容] {payload.text}")

    # 尝试解析嵌套JSON
    parsed_json = None
    try:
        parsed_json = json.loads(payload.text)
        print("[文本解析] text字段已解析为JSON格式")
    except Exception:
        print("[文本解析] text字段为普通文本")

    # 写入本地日志文件
    save_record = {
        "time": datetime.now().isoformat(timespec="seconds"),
        "source": payload.source,
        "text": payload.text,
        "parsed_json": parsed_json,
        "meta": payload.meta,
    }
    with open(TEXT_LOG_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(save_record, ensure_ascii=False) + "\n")

    # 向所有在线大屏广播数据
    broadcast_data = {
        "event": "a1_task_json",
        "text": payload.text,
        "source": payload.source,
        "meta": payload.meta,
        "parsed_json": parsed_json,
    }
    disconnected = []
    for ws in connected_dashboards:
        try:
            await ws.send_json(broadcast_data)
        except Exception:
            disconnected.append(ws)
    # 清理断开的连接
    for ws in disconnected:
        if ws in connected_dashboards:
            connected_dashboards.remove(ws)

    return {
        "status": "success",
        "message": "text received",
        "saved_to": str(TEXT_LOG_PATH.resolve()),
        "broadcast_count": len(connected_dashboards),
    }

# ====================== 接口2：大屏前端长连接控制通道 ======================
@app.websocket("/ws/dashboard_control")
async def dashboard_control_websocket(websocket: WebSocket):
    await websocket.accept()
    connected_dashboards.append(websocket)
    print(f"[大屏链路] 可视化大屏已接入，当前在线总数: {len(connected_dashboards)}")

    try:
        # 保持长连接，持续监听前端消息
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        if websocket in connected_dashboards:
            connected_dashboards.remove(websocket)
        print("[大屏链路] 大屏控制连接已断开")

# ====================== 接口3：接收业务控制信号 ======================
@app.post("/api/signal")
async def receive_server_signal(payload: ServerSignalPayload):
    print(f"[接收信号] 事件: {payload.event} | 提示文字: {payload.text} | 画面标记值: {payload.value}")

    # 广播数据结构
    broadcast_data = {
        "event": payload.event,
        "text": payload.text,
        "value": payload.value,
        "task_type": payload.task_type,
        "task_id": payload.task_id,
    }

    # 写入信号日志
    save_record = {
        "time": datetime.now().isoformat(timespec="seconds"),
        "event": payload.event,
        "text": payload.text,
        "value": payload.value,
        "task_type": payload.task_type,
        "task_id": payload.task_id,
    }
    with open(SIGNAL_LOG_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(save_record, ensure_ascii=False) + "\n")

    # 向所有大屏广播信号
    disconnected = []
    for ws in connected_dashboards:
        try:
            await ws.send_json(broadcast_data)
        except Exception:
            disconnected.append(ws)
    for ws in disconnected:
        if ws in connected_dashboards:
            connected_dashboards.remove(ws)

    return {
        "status": "success",
        "broadcast_count": len(connected_dashboards),
        "saved_to": str(SIGNAL_LOG_PATH)
    }

# ====================== 视频推流 WebSocket 接口 ======================
# 兼容旧接口 /ws/video → 存入 elder（保留兼容，优先级低于HTTP拉流）
@app.websocket("/ws/video")
async def receive_video_stream_legacy_elder(websocket: WebSocket):
    token = websocket.query_params.get("token")
    if token != API_KEY:
        await websocket.close(code=1008)
        return

    await websocket.accept()
    client_ip = websocket.client.host
    print(f"[视频推流] 设备IP:{client_ip} 接入旧接口 → 绑定【老人视角 elder】")

    try:
        while True:
            data = await websocket.receive_bytes()
            latest_frames["elder"] = data
    except WebSocketDisconnect:
        print(f"[视频断开] 老人视角设备({client_ip}) 离线")

# 机器人主视角推流接口 /ws/video2 → 存入 robot（原有逻辑完全保留）
@app.websocket("/ws/video2")
async def receive_video_stream_legacy_robot(websocket: WebSocket):
    token = websocket.query_params.get("token")
    if token != API_KEY:
        await websocket.close(code=1008)
        return

    await websocket.accept()
    client_ip = websocket.client.host
    print(f"[视频推流] 设备IP:{client_ip} 接入接口video2 → 绑定【机器人视角 robot】")

    try:
        while True:
            data = await websocket.receive_bytes()
            latest_frames["robot"] = data
    except WebSocketDisconnect:
        print(f"[视频断开] 机器人视角设备({client_ip}) 离线")

# 标准多通道接口 /ws/video/{camera_id} → 支持 elder / robot / grasp
@app.websocket("/ws/video/{camera_id}")
async def receive_video_stream_standard(websocket: WebSocket, camera_id: str):
    token = websocket.query_params.get("token")
    # 校验通道合法性
    if token != API_KEY or camera_id not in latest_frames:
        await websocket.close(code=1008)
        return

    await websocket.accept()
    client_ip = websocket.client.host
    print(f"[视频推流] 标准通道:{camera_id} | 来源IP:{client_ip} 已连接")

    try:
        while True:
            data = await websocket.receive_bytes()
            latest_frames[camera_id] = data
    except WebSocketDisconnect:
        print(f"[视频断开] 标准通道:{camera_id} 设备离线")

# ====================== 视频拉流 MJPEG 生成器 ======================
async def generate_mjpeg_stream(camera_id: str):
    """
    持续推送最新帧，过滤无效脏数据
    兼容静态画面、低帧率摄像头
    """
    while True:
        frame_bytes = latest_frames.get(camera_id)
        # 过滤无效帧
        if frame_bytes and len(frame_bytes) > 100:
            # 标准 MJPEG 流格式
            yield b'--frame\r\nContent-Type: image/jpeg\r\n\r\n' + frame_bytes + b'\r\n'
        # 控制输出帧率
        await asyncio.sleep(0.02)

# ====================== 前端HTTP拉流接口 ======================
@app.get("/stream/{camera_id}")
async def video_feed(camera_id: str):
    if camera_id not in latest_frames:
        raise HTTPException(status_code=404, detail="Video channel not found")
    return StreamingResponse(
        generate_mjpeg_stream(camera_id),
        media_type="multipart/x-mixed-replace; boundary=frame"
    )

# ====================== 程序入口 ======================
if __name__ == "__main__":
    import uvicorn
    # host=0.0.0.0 允许局域网所有设备访问
    # port=8001 固定端口
    # reload=False 关闭热重载，避免连接频繁断开
    uvicorn.run("stream_receiverV9:app", host="0.0.0.0", port=8001, reload=False)
