"""카카오 로컬 API 를 흉내 내는 가짜 서버. 실제 카카오를 부르지 않고 지연·오류 응답을 재현한다.

  python fake_kakao.py            # 18200 포트
  curl "http://localhost:18200/__mode?m=delay:5"

모드: ok / delay:<초> / status:<코드> / badjson / empty / close(응답 없이 연결 끊기) / hang(60초 동안 응답 안 함)
/__stats 는 지금까지 받은 호출 수를 돌려주고, /__reset 은 0 으로 되돌린다.
"""
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

STATE = {"mode": "ok", "calls": 0}
LOCK = threading.Lock()
REGION = {"documents": [{"region_type": "B", "code": "1111010100", "region_3depth_name": "측정동",
                         "region_4depth_name": ""}]}
ADDRESS = {"documents": [{"road_address": {"address_name": "측정시 측정구 측정로 1", "building_name": "측정빌딩"},
                          "address": {"address_name": "측정시 측정구 측정동 1"}}]}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_):
        pass

    def reply(self, status, body):
        data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json;charset=UTF-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        url = urlparse(self.path)
        if url.path == "/__mode":
            STATE["mode"] = parse_qs(url.query)["m"][0]
            return self.reply(200, {"mode": STATE["mode"]})
        if url.path == "/__stats":
            return self.reply(200, dict(STATE))
        if url.path == "/__reset":
            STATE["calls"] = 0
            return self.reply(200, dict(STATE))
        with LOCK:
            STATE["calls"] += 1
        mode = STATE["mode"]
        if mode.startswith("delay:"):
            time.sleep(float(mode.split(":")[1]))
        elif mode.startswith("status:"):
            return self.reply(int(mode.split(":")[1]), {"msg": "fake error"})
        elif mode == "badjson":
            return self.reply(200, b"{not json")
        elif mode == "empty":
            return self.reply(200, b"")
        elif mode == "close":
            self.close_connection = True
            return self.connection.close()
        elif mode == "hang":
            time.sleep(60)
        self.reply(200, REGION if "coord2regioncode" in url.path else ADDRESS)


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 18200), Handler).serve_forever()
