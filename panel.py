#!/usr/bin/env python3
import html
import http.client
import json
import logging
import os
import secrets
import sqlite3
import socket
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs

DB = os.environ.get("PANEL_DB", "/data/panel.db")
ADMIN_PASSWORD = os.environ["ADMIN_PASSWORD"]
PUBLIC_IP = os.environ.get("PUBLIC_IP", "公网IP待配置")
PORT = int(os.environ.get("PANEL_PORT", "8080"))
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
db = sqlite3.connect(DB, check_same_thread=False, timeout=30)
db.execute("pragma journal_mode=WAL")
db.execute("create table if not exists licenses (code text primary key, status text not null default 'new', port integer unique)")
columns = {row[1] for row in db.execute("pragma table_info(licenses)")}
if "days" not in columns:
    db.execute("alter table licenses add column days integer not null default 30")
if "expires_at" not in columns:
    db.execute("alter table licenses add column expires_at text")
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


def delete_radar(name):
    connection = DockerConnection()
    connection.request("GET", f"/v1.41/containers/{name}/json")
    response = connection.getresponse()
    data = response.read()
    if response.status == 404:
        connection.close()
        return
    if response.status != 200:
        raise RuntimeError(data.decode(errors="replace"))
    connection.request("DELETE", f"/v1.41/containers/{name}?force=true&v=true")
    response = connection.getresponse()
    data = response.read()
    if response.status not in (204, 404):
        raise RuntimeError(data.decode(errors="replace"))
    connection.close()


def expire_rentals():
    while True:
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        due = db.execute("select code,port from licenses where status='used' and expires_at<=?", (now,)).fetchall()
        for code, port in due:
            try:
                delete_radar(f"nexoradar-{port - 9000}")
                with db:
                    db.execute("update licenses set status='expired' where code=? and status='used'", (code,))
            except (RuntimeError, OSError):
                pass
        time.sleep(30)


STYLE = '''<style>
*{box-sizing:border-box}body{margin:0;min-height:100vh;background:#0b1114;color:#e7f0ef;font:15px/1.5 system-ui,"Microsoft YaHei",sans-serif;background-image:radial-gradient(ellipse at 50% -20%,#173c3b 0,transparent 55%)}
.wrap{width:min(100% - 32px,1100px);margin:5vh auto}.wrap:has(form[action="/redeem"]){max-width:760px}.top{display:flex;align-items:center;gap:12px;margin-bottom:30px}.mark{width:38px;height:38px;border:1px solid #52d7ca;border-radius:50%;position:relative;box-shadow:0 0 24px #52d7ca33}.mark:before{content:"";position:absolute;inset:8px;border:1px solid #52d7ca88;border-radius:50%}.mark:after{content:"";position:absolute;width:6px;height:6px;left:15px;top:15px;background:#52d7ca;border-radius:50%}.brand{font-weight:700;letter-spacing:.08em}.sub{color:#90a5a2;font-size:12px}.card{background:#111b1e;border:1px solid #28383a;border-radius:18px;padding:clamp(20px,4vw,34px);box-shadow:0 20px 70px #0005}h1{font-size:clamp(26px,4vw,34px);margin:0 0 8px}.muted{color:#9aacab;margin:0 0 22px}label{display:block;margin:14px 0 7px;color:#bfd1ce}input,textarea,button{font:inherit;border-radius:9px;padding:11px 13px}input:not([type=checkbox]),textarea{background:#0b1315;border:1px solid #34484a;color:#e7f0ef}input[type=checkbox]{width:18px;height:18px;accent-color:#52d7ca;cursor:pointer}textarea{resize:vertical;min-height:130px}button{margin:8px 6px 8px 0;border:0;background:#52d7ca;color:#06201e;font-weight:700;cursor:pointer}button:hover{background:#76e6da}.result{white-space:pre-wrap;overflow-wrap:anywhere;color:#9ce6d9;margin-top:18px}.nav{display:flex;gap:18px;margin-top:22px;color:#8da5a2;font-size:13px}.nav a{color:inherit;text-decoration:none}.nav a:hover{color:#52d7ca}.tools{display:grid;grid-template-columns:1fr 1fr;gap:10px}.table-wrap{overflow:auto;margin-top:22px}table{width:100%;border-collapse:collapse;min-width:720px}th,td{text-align:left;padding:11px 9px;border-bottom:1px solid #28383a}th{color:#9aacab;font-size:13px}td{font-size:14px}td code{color:#9ce6d9;overflow-wrap:anywhere}.delete{background:#873d42;color:white}.toolbar{display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap;margin-top:20px}.toolbar button{margin:0}.status{display:inline-block;padding:3px 9px;border:1px solid #477d70;border-radius:999px;color:#8ee3c2;font-size:12px}form[action="/redeem"] textarea{display:block;width:100%;min-height:150px}form[action="/redeem"] button{display:block;min-width:150px;margin:14px 0 0}@media(max-width:520px){.tools{grid-template-columns:1fr}}
</style>'''


def page(mode="redeem", message=""):
    if mode == "login":
        content = f'''<h1>管理员登录</h1><p class="muted">登录后管理卡密和兑换实例。</p><form method="post" action="/admin"><label>管理员密码</label><input name="password" type="password" required autocomplete="current-password"><button>登录</button></form><div class="result">{html.escape(message)}</div>'''
    elif mode == "admin":
        rows = db.execute("select code,status,port,days,expires_at from licenses order by rowid desc limit 500").fetchall()
        status_names = {"used": "已兑换", "expired": "已到期", "creating": "开通中", "new": "未使用"}
        table_rows = "".join(f'''<tr><td><input class="license-check" type="checkbox" name="codes" value="{html.escape(code)}" form="manage" aria-label="选择卡密"></td><td><code>{html.escape(code)}</code></td><td>{days} 天</td><td><span class="status">{status_names.get(status, status)}</span></td><td>{('nexoradar-' + str(port - 9000)) if port else '—'}</td><td>{(PUBLIC_IP + ':' + str(port)) if port else '—'}</td><td>{expires or '兑换后开始计时'}</td><td><button type="submit" class="delete" form="delete-{html.escape(code)}">删除</button><form id="delete-{html.escape(code)}" method="post" action="/admin"><input type="hidden" name="action" value="delete"><input type="hidden" name="code" value="{html.escape(code)}"></form></td></tr>''' for code, status, port, days, expires in rows)
        content = f'''<h1>卡密管理</h1><p class="muted">登录后可直接查看卡密、套餐时长和对应雷达实例。</p>
<form method="post" action="/admin"><input type="hidden" name="action" value="generate"><label>生成数量与有效时长</label><div class="tools"><input name="count" type="number" min="1" max="100" value="1" aria-label="生成数量"><input name="days" type="number" min="1" max="3650" value="30" aria-label="有效天数"></div><button>生成卡密</button></form><div class="result">{html.escape(message)}</div>
<h2>卡密列表 <span class="muted">最近 {len(rows)} 张</span></h2><form id="manage" method="post" action="/admin"><input type="hidden" name="action" value="delete_selected"></form><div class="toolbar"><label style="margin:0"><input id="select-all" type="checkbox"> 全选本页</label><button class="delete" type="submit" form="manage">删除所选</button></div><div class="table-wrap"><table><thead><tr><th>选择</th><th>卡密</th><th>时长</th><th>状态</th><th>雷达容器</th><th>连接地址</th><th>到期时间</th><th>操作</th></tr></thead><tbody>{table_rows or '<tr><td colspan="8">暂无卡密</td></tr>'}</tbody></table></div><form method="post" action="/logout"><button>退出登录</button></form><script>const all=document.getElementById('select-all');all.addEventListener('change',()=>document.querySelectorAll('.license-check').forEach(box=>box.checked=all.checked));document.querySelectorAll('.license-check').forEach(box=>box.addEventListener('change',()=>{{const boxes=[...document.querySelectorAll('.license-check')];all.checked=boxes.length>0&&boxes.every(item=>item.checked);all.indeterminate=boxes.some(item=>item.checked)&&!all.checked}}));</script>'''
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
            action = form.get("action", [""])[0]
            if action == "delete":
                codes = form.get("code", [])
                with db:
                    for code in codes:
                        row = db.execute("select port from licenses where code=?", (code,)).fetchone()
                        if row and row[0]:
                            delete_radar(f"nexoradar-{row[0] - 9000}")
                        db.execute("delete from licenses where code=?", (code,))
                return self.send_page(page("admin", f"已删除 {len(codes)} 张卡密及其关联实例。"))
            if action == "delete_selected":
                codes = form.get("codes", [])
                with db:
                    for code in codes:
                        row = db.execute("select port from licenses where code=?", (code,)).fetchone()
                        if row and row[0]:
                            delete_radar(f"nexoradar-{row[0] - 9000}")
                        db.execute("delete from licenses where code=?", (code,))
                return self.send_page(page("admin", f"已删除 {len(codes)} 张卡密及其关联实例。"))
            try:
                count = max(1, min(100, int(form.get("count", ["1"])[0])))
                days = max(1, min(3650, int(form.get("days", ["30"])[0])))
            except ValueError:
                count = 1
                days = 30
            codes = [secrets.token_urlsafe(18) for _ in range(count)]
            with db:
                db.executemany("insert into licenses(code,days) values(?,?)", [(code, days) for code in codes])
            return self.send_page(page("admin", f"已生成 {count} 张卡密，有效期 {days} 天。\n" + "\n".join(codes)))

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
                    row = db.execute("select status,port,days,expires_at from licenses where code=?", (code,)).fetchone()
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
                    expiry = (datetime.now(timezone.utc) + timedelta(days=row[2])).isoformat(timespec="seconds")
                    with db:
                        db.execute("update licenses set status='used',expires_at=? where code=?", (expiry, code))
                    results.append(f"{code}: {PUBLIC_IP}:{port}（有效 {row[2]} 天）")
                except (RuntimeError, OSError, ValueError) as error:
                    logging.exception("Radar container creation failed for license %s on port %s", code, port)
                    with db:
                        db.execute("update licenses set status='new',port=null where code=?", (code,))
                    results.append(f"{code}: 创建失败。请联系管理员并提供端口 {port} 和时间。")
            return self.send_page(page("redeem", "\n".join(results)))
        self.send_page("Not found", 404)


if __name__ == "__main__":
    threading.Thread(target=expire_rentals, daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
