#!/usr/bin/env python3
import html
import http.client
import json
import os
import secrets
import sqlite3
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs

DB = os.environ.get("PANEL_DB", "/data/panel.db")
ADMIN_PASSWORD = os.environ["ADMIN_PASSWORD"]
PUBLIC_IP = os.environ.get("PUBLIC_IP", "公网IP待配置")
PORT = int(os.environ.get("PANEL_PORT", "8080"))
db = sqlite3.connect(DB, check_same_thread=False, timeout=30)
db.execute("pragma journal_mode=WAL")
db.execute("create table if not exists licenses (code text primary key, status text not null default 'new', port integer unique)")
db.commit()
ADMIN_SESSIONS = set()
SESSION_LOCK = threading.Lock()


class DockerConnection(http.client.HTTPConnection):
    def __init__(self):
        super().__init__("localhost")

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.connect("/var/run/docker.sock")


def create_radar(name, port):
    connection = DockerConnection()
    body = json.dumps({
        "Image": "nexoradar:latest",
        "ExposedPorts": {"9000/tcp": {}},
        "HostConfig": {
            "PortBindings": {"9000/tcp": [{"HostPort": str(port)}]},
            "RestartPolicy": {"Name": "unless-stopped"},
        },
    })
    connection.request("POST", f"/v1.41/containers/create?name={name}", body, {"Content-Type": "application/json"})
    response = connection.getresponse()
    data = response.read()
    if response.status not in (201,):
        raise RuntimeError(data.decode(errors="replace"))
    container_id = json.loads(data)["Id"]
    connection.request("POST", f"/v1.41/containers/{container_id}/start")
    response = connection.getresponse()
    data = response.read()
    if response.status not in (204,):
        raise RuntimeError(data.decode(errors="replace"))
    connection.close()


STYLE = '''<style>
*{box-sizing:border-box}body{margin:0;min-height:100vh;background:#0b1114;color:#e7f0ef;font:15px/1.5 system-ui,"Microsoft YaHei",sans-serif;background-image:radial-gradient(ellipse at 50% -20%,#173c3b 0,transparent 55%)}
.wrap{width:min(100% - 32px,760px);margin:7vh auto}.top{display:flex;align-items:center;gap:12px;margin-bottom:36px}.mark{width:38px;height:38px;border:1px solid #52d7ca;border-radius:50%;position:relative;box-shadow:0 0 24px #52d7ca33}.mark:before{content:"";position:absolute;inset:8px;border:1px solid #52d7ca88;border-radius:50%}.mark:after{content:"";position:absolute;width:6px;height:6px;left:15px;top:15px;background:#52d7ca;border-radius:50%}.brand{font-weight:700;letter-spacing:.08em}.sub{color:#90a5a2;font-size:12px}.card{background:#111b1e;border:1px solid #28383a;border-radius:18px;padding:clamp(22px,5vw,40px);box-shadow:0 20px 70px #0005}h1{font-size:clamp(28px,5vw,38px);margin:0 0 8px;letter-spacing:-.04em}.muted{color:#9aacab;margin:0 0 28px}label{display:block;margin:18px 0 8px;color:#bfd1ce}input,textarea,button{font:inherit;width:100%;border-radius:10px;padding:13px 15px}input,textarea{background:#0b1315;border:1px solid #34484a;color:#e7f0ef}textarea{resize:vertical;min-height:130px}input:focus,textarea:focus{outline:2px solid #52d7ca66;border-color:#52d7ca}button{margin-top:14px;border:0;background:#52d7ca;color:#06201e;font-weight:700;cursor:pointer}button:hover{background:#76e6da}.result{white-space:pre-wrap;overflow-wrap:anywhere;color:#9ce6d9;margin-top:18px}.nav{display:flex;gap:18px;margin-top:22px;color:#8da5a2;font-size:13px}.nav a{color:inherit;text-decoration:none}.nav a:hover{color:#52d7ca}.tools{display:grid;grid-template-columns:1fr 1fr;gap:10px}@media(max-width:520px){.tools{grid-template-columns:1fr}}
</style>'''


def page(mode="redeem", message=""):
    if mode == "login":
        content = f'''<h1>管理员登录</h1><p class="muted">登录后管理卡密和兑换实例。</p><form method="post" action="/admin"><label>管理员密码</label><input name="password" type="password" required autocomplete="current-password"><button>登录</button></form><div class="result">{html.escape(message)}</div>'''
    elif mode == "admin":
        content = f'''<h1>卡密管理</h1><p class="muted">生成兑换凭证，或查看最近的兑换记录。</p>
<form method="post" action="/admin"><label>生成数量</label><input name="count" type="number" min="1" max="100" value="1"><div class="tools"><button name="action" value="generate">生成卡密</button><button name="action" value="list">查看兑换记录</button></div></form><form method="post" action="/logout"><button>退出登录</button></form><div class="result">{html.escape(message)}</div>'''
    else:
        content = f'''<h1>连接共享雷达</h1><p class="muted">输入卡密即可开通雷达接入地址。支持多张卡密，每行一张。</p>
<form method="post" action="/redeem"><label>兑换卡密</label><textarea name="codes" placeholder="请粘贴卡密，每行一张" required></textarea><button>验证并开通</button></form><div class="result">{html.escape(message)}</div>'''
    nav = '<nav class="nav"><a href="/">客户兑换</a></nav>' if mode in ("admin", "login") else ""
    return f'''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>DQ 雷达</title>{STYLE}<main class="wrap"><header class="top"><span class="mark"></span><div><div class="brand">DQ RADAR</div><div class="sub">共享雷达接入服务</div></div></header><section class="card">{content}</section>{nav}</main></html>'''


class Handler(BaseHTTPRequestHandler):
    def send_page(self, content, status=200):
        body = content.encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/admin":
            token = self.headers.get("Cookie", "").removeprefix("dq_session=")
            with SESSION_LOCK:
                authenticated = token in ADMIN_SESSIONS
            return self.send_page(page("admin" if authenticated else "login"))
        self.send_page(page())

    def do_POST(self):
        form = parse_qs(self.rfile.read(int(self.headers.get("Content-Length", "0"))).decode())
        if self.path == "/admin":
            token = self.headers.get("Cookie", "").removeprefix("dq_session=")
            with SESSION_LOCK:
                authenticated = token in ADMIN_SESSIONS
            if not authenticated:
                password = form.get("password", [""])[0]
                if not secrets.compare_digest(password, ADMIN_PASSWORD):
                    return self.send_page(page("login", "管理员密码错误"), 403)
                token = secrets.token_urlsafe(32)
                with SESSION_LOCK:
                    ADMIN_SESSIONS.add(token)
                self.send_response(303)
                self.send_header("Location", "/admin")
                self.send_header("Set-Cookie", f"dq_session={token}; HttpOnly; SameSite=Strict; Path=/admin")
                self.end_headers()
                return
            if form.get("action", [""])[0] == "list":
                rows = db.execute("select code,status,port from licenses order by rowid desc limit 200").fetchall()
                listing = "\n".join(
                    f"卡密：{code} | 状态：{status} | 容器：{('nexoradar-' + str(port - 9000)) if port else '未创建'} | 地址：{(PUBLIC_IP + ':' + str(port)) if port else '-'}"
                    for code, status, port in rows
                ) or "暂无卡密"
                return self.send_page(page("admin", listing))
            try:
                count = max(1, min(100, int(form.get("count", ["1"])[0])))
            except ValueError:
                count = 1
            codes = [secrets.token_urlsafe(18) for _ in range(count)]
            with db:
                db.executemany("insert into licenses(code) values(?)", [(code,) for code in codes])
            return self.send_page(page("admin", "\n".join(codes)))

        if self.path == "/logout":
            token = self.headers.get("Cookie", "").removeprefix("dq_session=")
            with SESSION_LOCK:
                ADMIN_SESSIONS.discard(token)
            self.send_response(303)
            self.send_header("Location", "/admin")
            self.send_header("Set-Cookie", "dq_session=; Max-Age=0; HttpOnly; Path=/admin")
            self.end_headers()
            return

        if self.path == "/redeem":
            results = []
            for code in dict.fromkeys(line.strip() for line in form.get("codes", [""])[0].splitlines() if line.strip()):
                with db:
                    db.execute("begin immediate")
                    row = db.execute("select status,port from licenses where code=?", (code,)).fetchone()
                    if row is None:
                        results.append(f"{code}: 卡密无效")
                        continue
                    if row[0] == "used":
                        results.append(f"{code}: 已兑换 {PUBLIC_IP}:{row[1]}")
                        continue
                    port = db.execute("select coalesce(max(port),9000)+1 from licenses").fetchone()[0]
                    db.execute("update licenses set status='creating',port=? where code=?", (port, code))
                name = f"nexoradar-{port - 9000}"
                try:
                    create_radar(name, port)
                    with db:
                        db.execute("update licenses set status='used' where code=?", (code,))
                    results.append(f"{code}: {PUBLIC_IP}:{port}")
                except (RuntimeError, OSError, ValueError):
                    with db:
                        db.execute("update licenses set status='new',port=null where code=?", (code,))
                    results.append(f"{code}: 创建失败，请稍后重试或联系管理员")
            return self.send_page(page("redeem", "\n".join(results)))
        self.send_page("Not found", 404)


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
