#!/usr/bin/env python3
import html
import http.client
import json
import os
import secrets
import sqlite3
import socket
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


def page(message="", admin_message=""):
    return f'''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>DQ雷达</title>
<style>body{{font:16px sans-serif;background:#101511;color:#eee;max-width:720px;margin:7vh auto;padding:20px}}input,textarea,button{{font:inherit;padding:12px;margin:6px 0;width:100%;box-sizing:border-box}}button{{background:#52d7ca;border:0;cursor:pointer}}pre{{white-space:pre-wrap;word-break:break-all}}</style>
<h1>DQ 雷达租用</h1><form method="post" action="/redeem"><label>卡密（每行一张，可多张同时兑换）</label><textarea name="codes" rows="6" required></textarea><button>兑换</button></form><pre>{html.escape(message)}</pre>
<hr><h2>管理卡密</h2><form method="post" action="/admin"><input name="password" type="password" placeholder="管理员密码" required><input name="count" type="number" min="1" max="100" value="1"><button name="action" value="generate">生成卡密</button><button name="action" value="list">查看卡密状态</button></form><pre>{html.escape(admin_message)}</pre></html>'''


class Handler(BaseHTTPRequestHandler):
    def send_page(self, content, status=200):
        body = content.encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self.send_page(page())

    def do_POST(self):
        form = parse_qs(self.rfile.read(int(self.headers.get("Content-Length", "0"))).decode())
        if self.path == "/admin":
            password = form.get("password", [""])[0]
            if not secrets.compare_digest(password, ADMIN_PASSWORD):
                return self.send_page(page(admin_message="管理员密码错误"), 403)
            if form.get("action", [""])[0] == "list":
                rows = db.execute("select code,status,port from licenses order by rowid desc limit 200").fetchall()
                listing = "\n".join(f"{code}  {status}  {PUBLIC_IP}:{port}" for code, status, port in rows) or "暂无卡密"
                return self.send_page(page(admin_message=listing))
            try:
                count = max(1, min(100, int(form.get("count", ["1"])[0])))
            except ValueError:
                count = 1
            codes = [secrets.token_urlsafe(18) for _ in range(count)]
            with db:
                db.executemany("insert into licenses(code) values(?)", [(code,) for code in codes])
            return self.send_page(page(admin_message="\n".join(codes)))

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
            return self.send_page(page(message="\n".join(results)))
        self.send_page("Not found", 404)


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
