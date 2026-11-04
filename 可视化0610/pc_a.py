"""电脑 A：本机网页 + WebRTC 信令服务器。"""

import asyncio
import json
import socket

from aiohttp import WSMsgType, web


PAGE = r"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>视频通话</title>
  <style>
    body{font-family:system-ui;margin:0;background:#111827;color:#f9fafb}
    main{max-width:1000px;margin:auto;padding:24px}.bar{display:flex;gap:10px;flex-wrap:wrap;align-items:center}
    input,button{font:inherit;padding:10px 13px;border-radius:8px;border:1px solid #4b5563}
    input{background:#1f2937;color:white}button{cursor:pointer;background:#2563eb;color:white;border:0}
    button:disabled{opacity:.5}.videos{display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-top:20px}
    video{width:100%;background:#000;border-radius:12px;aspect-ratio:16/9;object-fit:cover}
    .status{margin-top:12px;color:#93c5fd}@media(max-width:700px){.videos{grid-template-columns:1fr}}
main{max-width:960px}
.videos{display:block;position:relative;margin-top:20px;height:54vh;min-height:280px;max-height:540px;background:#030712;border:1px solid #374151;border-radius:16px;overflow:hidden}
.videos section{margin:0}
.videos .remote-view{position:absolute;inset:0}
.videos video{display:block;width:100%;height:100%;aspect-ratio:auto;border-radius:0;background:#030712}
.remote-view video{object-fit:cover;object-position:center}
.videos h3{position:absolute;top:12px;left:12px;z-index:1;margin:0;padding:5px 10px;border-radius:6px;background:rgba(17,24,39,.75);font-size:13px;font-weight:500}
.videos .local-view{position:absolute;right:18px;bottom:18px;width:clamp(110px,16vw,170px);aspect-ratio:16/9;z-index:2;overflow:hidden;border:2px solid #64748b;border-radius:12px;box-shadow:0 8px 30px #0009}
.local-view video{object-fit:cover}
.local-view h3{top:6px;left:6px;font-size:11px;padding:3px 7px}
@media(max-width:700px){main{padding:14px}.videos{height:46vh;min-height:240px}.videos .local-view{right:10px;bottom:10px;width:125px}.bar input,.bar select{max-width:100%;box-sizing:border-box}}
  </style>
</head>
<body><main>
  <h1>视频通话</h1>
  <div class="bar">
    <label>房间名 <input id="room" value="demo-room"></label>
    <button id="start">开启音视频并加入</button>
    <button id="hangup" disabled>挂断</button>
  </div>
  <div id="status" class="status">等待开始</div>
  <div class="videos">
    <section class="remote-view"><h3>对方</h3><video id="remote" autoplay playsinline></video></section>
    <section class="local-view"><h3>本机预览</h3><video id="local" autoplay muted playsinline></video></section>
  </div>
</main>
<script>
const $ = id => document.getElementById(id);
let ws, pc, stream, pendingCandidates = [];
const rtcConfig = {iceServers:[{urls:'stun:stun.l.google.com:19302'},{urls:'stun:stun.cloudflare.com:3478'}]};
function status(s){$('status').textContent=s}
function send(data){if(ws?.readyState===WebSocket.OPEN) ws.send(JSON.stringify(data))}

async function makePeer(){
  pc = new RTCPeerConnection(rtcConfig);
  stream.getTracks().forEach(t => pc.addTrack(t, stream));
  pc.ontrack = e => {$('remote').srcObject=e.streams[0]};
  pc.onicecandidate = e => {if(e.candidate) send({type:'candidate',candidate:e.candidate})};
  pc.onconnectionstatechange = () => {
    const s=pc.connectionState;
    status(s==='connected'?'已连接，正在点对点通话':'连接状态：'+s);
  };
}

async function start(){
  $('start').disabled=true;
  try{
    stream=await navigator.mediaDevices.getUserMedia({video:true,audio:true});
    $('local').srcObject=stream;
    await makePeer();
    ws=new WebSocket('ws://127.0.0.1:8765/ws');
    ws.onopen=()=>{send({type:'join',room:$('room').value.trim()||'demo-room'});status('已进入房间，等待另一台电脑')};
    ws.onmessage=async e=>{
      const m=JSON.parse(e.data);
      if(m.type==='error'){throw new Error(m.message)}
      if(m.type==='ready' && m.initiator){
        const offer=await pc.createOffer(); await pc.setLocalDescription(offer);
        send({type:'offer',sdp:pc.localDescription}); status('正在呼叫对方…');
      } else if(m.type==='offer'){
        await pc.setRemoteDescription(m.sdp);
        const answer=await pc.createAnswer(); await pc.setLocalDescription(answer);
        send({type:'answer',sdp:pc.localDescription}); await flushCandidates();
      } else if(m.type==='answer'){
        await pc.setRemoteDescription(m.sdp); await flushCandidates();
      } else if(m.type==='candidate'){
        if(pc.remoteDescription) await pc.addIceCandidate(m.candidate);
        else pendingCandidates.push(m.candidate);
      } else if(m.type==='peer-left'){status('对方已离开')}
    };
    ws.onerror=()=>status('信令连接发生错误'); ws.onclose=e=>{if(pc?.connectionState!=='connected')status(`信令连接已关闭（代码 ${e.code}${e.reason?'：'+e.reason:''}）`)};
    $('hangup').disabled=false;
  }catch(err){status('启动失败：'+err.message);hangup()}
}
async function flushCandidates(){for(const c of pendingCandidates)await pc.addIceCandidate(c);pendingCandidates=[]}
function hangup(){ws?.close();pc?.close();stream?.getTracks().forEach(t=>t.stop());$('local').srcObject=null;$('remote').srcObject=null;$('start').disabled=false;$('hangup').disabled=true}
$('start').onclick=start;$('hangup').onclick=()=>{hangup();status('已挂断')};
</script></body></html>"""


rooms: dict[str, list[web.WebSocketResponse]] = {}
DISCOVERY_MESSAGE = b"WEBRTC_CALL_DISCOVER_V1"


class DiscoveryProtocol(asyncio.DatagramProtocol):
    """回应局域网内电脑 B 发出的服务发现广播。"""

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data, addr):
        if data == DISCOVERY_MESSAGE:
            response = json.dumps(
                {"service": "webrtc-call-v1", "name": socket.gethostname(), "port": 8765}
            ).encode("utf-8")
            self.transport.sendto(response, addr)


async def index(_: web.Request) -> web.Response:
    return web.Response(text=PAGE, content_type="text/html")


async def websocket_handler(request: web.Request) -> web.WebSocketResponse:
    ws = web.WebSocketResponse(heartbeat=30)
    await ws.prepare(request)
    room_name = None
    remote = request.remote or "unknown"
    print(f"[信令] {remote} 已连接")
    try:
        async for message in ws:
            if message.type != WSMsgType.TEXT:
                continue
            data = json.loads(message.data)
            if data.get("type") == "join":
                room_name = str(data.get("room", "demo-room"))[:100]
                peers = rooms.setdefault(room_name, [])
                if len(peers) >= 2:
                    await ws.send_json({"type": "error", "message": "该房间已有两人"})
                    await ws.close()
                    break
                peers.append(ws)
                print(f"[信令] {remote} 加入房间 {room_name}（当前 {len(peers)} 人）")
                if len(peers) == 2:
                    await peers[0].send_json({"type": "ready", "initiator": True})
                    await peers[1].send_json({"type": "ready", "initiator": False})
            elif room_name:
                for peer in rooms.get(room_name, []):
                    if peer is not ws and not peer.closed:
                        await peer.send_json(data)
    except Exception as exc:
        print(f"[信令] {remote} 异常：{type(exc).__name__}: {exc}")
    finally:
        if room_name and room_name in rooms:
            peers = rooms[room_name]
            if ws in peers:
                peers.remove(ws)
            for peer in peers:
                if not peer.closed:
                    await peer.send_json({"type": "peer-left"})
            if not peers:
                rooms.pop(room_name, None)
        print(f"[信令] {remote} 已断开")
    return ws


async def main() -> None:
    app = web.Application()
    app.router.add_get("/", index)
    app.router.add_get("/ws", websocket_handler)
    runner = web.AppRunner(app)
    await runner.setup()
    # 同一应用同时监听：localhost 页面端口，以及供 B 连接的局域网端口。
    local_site = web.TCPSite(runner, "127.0.0.1", 8080)
    signal_site = web.TCPSite(runner, "0.0.0.0", 8765)
    await local_site.start()
    await signal_site.start()
    loop = asyncio.get_running_loop()
    discovery_transport, _ = await loop.create_datagram_endpoint(
        DiscoveryProtocol,
        local_addr=("0.0.0.0", 8766),
        allow_broadcast=True,
    )
    try:
        ip = socket.gethostbyname(socket.gethostname())
    except OSError:
        ip = "电脑A的局域网IP"
    print("电脑 A 页面：http://127.0.0.1:8080")
    print(f"本机局域网地址：{ip}")
    print("已开启局域网自动发现（UDP 8766）")
    print("按 Ctrl+C 退出")
    try:
        await asyncio.Event().wait()
    finally:
        discovery_transport.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
