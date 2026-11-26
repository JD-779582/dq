#!/usr/bin/env python3
import html
import http.client
import json
import logging
import os
import random
import secrets
import sqlite3
import socket
import threading
import time
from http.cookies import SimpleCookie
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs

DB = os.environ.get("PANEL_DB", "/data/panel.db")
ADMIN_PASSWORD = os.environ["ADMIN_PASSWORD"]
PUBLIC_IP = os.environ.get("PUBLIC_IP", "ld.852079.xyz")
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
if "radar_password" not in columns:
    db.execute("alter table licenses add column radar_password text")
db.commit()
ADMIN_SESSIONS = set()
SESSION_LOCK = threading.Lock()
REDEEM_RESULTS = {}


class DockerConnection(http.client.HTTPConnection):
    def __init__(self):
        super().__init__("localhost")

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.connect("/var/run/docker.sock")


def create_radar(name, port, radar_password):
    connection = DockerConnection()
    body = json.dumps({
        "Image": "nexoradar:latest",
        "Env": [f"pwd={radar_password}"],
        "ExposedPorts": {"9000/tcp": {}},
        "HostConfig": {
            "PortBindings": {"9000/tcp": [{"HostPort": str(port)}]},
            "RestartPolicy": {"Name": "unless-stopped"},
        },
    })
    connection.request("POST", f"/v1.44/containers/create?name={name}", body, {"Content-Type": "application/json"})
    response = connection.getresponse()
    data = response.read()
    if response.status not in (201,):
        raise RuntimeError(data.decode(errors="replace"))
    container_id = json.loads(data)["Id"]
    connection.request("POST", f"/v1.44/containers/{container_id}/start")
    response = connection.getresponse()
    data = response.read()
    if response.status not in (204,):
        raise RuntimeError(data.decode(errors="replace"))
    connection.close()


def delete_radar(name):
    connection = DockerConnection()
    connection.request("GET", f"/v1.44/containers/{name}/json")
    response = connection.getresponse()
    data = response.read()
    if response.status == 404:
        connection.close()
        return
    if response.status != 200:
        raise RuntimeError(data.decode(errors="replace"))
    connection.request("DELETE", f"/v1.44/containers/{name}?force=true&v=true")
    response = connection.getresponse()
    data = response.read()
    if response.status not in (204, 404):
        raise RuntimeError(data.decode(errors="replace"))
    connection.close()


def radar_container_for_port(port):
    connection = DockerConnection()
    connection.request("GET", "/v1.44/containers/json?all=1")
    response = connection.getresponse()
    data = response.read()
    connection.close()
    if response.status != 200:
        raise RuntimeError(data.decode(errors="replace"))
    for container in json.loads(data):
        if any(item.get("PublicPort") == port for item in container.get("Ports", [])):
            names = container.get("Names", [])
            return names[0].lstrip("/") if names else container["Id"][:12]
    return None


def format_expiry(value):
    if not value:
        return "兑换后开始计时"
    try:
        expiry = datetime.fromisoformat(value)
        if expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=timezone.utc)
        return expiry.astimezone(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError):
        return value


def next_radar_slot():
    connection = DockerConnection()
    connection.request("GET", "/v1.44/containers/json?all=1")
    response = connection.getresponse()
    data = response.read()
    connection.close()
    if response.status != 200:
        raise RuntimeError(data.decode(errors="replace"))

    containers = json.loads(data)
    occupied_ports = set()
    occupied_names = set()
    for container in containers:
        occupied_names.update(name.lstrip("/") for name in container.get("Names", []))
        for binding in container.get("Ports", []):
            host_port = binding.get("PublicPort")
            if host_port:
                occupied_ports.add(host_port)

    reserved_ports = {row[0] for row in db.execute("select port from licenses where port is not null")}
    available = [
        port for port in range(9000, 25001)
        if port not in occupied_ports
        and port not in reserved_ports
        and f"nexoradar-{port - 9000}" not in occupied_names
    ]
    if not available:
        raise RuntimeError("No available host ports in range 9000-25000")
    return random.choice(available)


def expire_rentals():
    while True:
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        due = db.execute("select code,port from licenses where status='used' and expires_at<=?", (now,)).fetchall()
        for code, port in due:
            try:
                container_name = radar_container_for_port(port)
                if container_name:
                    delete_radar(container_name)
                with db:
                    db.execute("update licenses set status='expired' where code=? and status='used'", (code,))
            except (RuntimeError, OSError):
                pass
        time.sleep(30)


STYLE = '''<style>
*{box-sizing:border-box}body{margin:0;min-height:100vh;background:#0b1114;color:#e7f0ef;font:15px/1.5 system-ui,"Microsoft YaHei",sans-serif;background-image:radial-gradient(ellipse at 50% -20%,#173c3b 0,transparent 55%)}
.wrap{width:min(100% - 32px,1100px);margin:5vh auto}.wrap:has(form[action="/redeem"]){max-width:820px}.wrap:has(form[action="/admin"] input[name="password"]){max-width:560px}.top{display:flex;align-items:center;gap:12px;margin-bottom:30px}.mark{width:38px;height:38px;border:1px solid #52d7ca;border-radius:50%;position:relative;box-shadow:0 0 24px #52d7ca33}.mark:before{content:"";position:absolute;inset:8px;border:1px solid #52d7ca88;border-radius:50%}.mark:after{content:"";position:absolute;width:6px;height:6px;left:15px;top:15px;background:#52d7ca;border-radius:50%}.brand{font-weight:700;letter-spacing:.08em}.sub{color:#90a5a2;font-size:12px}.card{background:#111b1e;border:1px solid #28383a;border-radius:18px;padding:clamp(20px,4vw,34px);box-shadow:0 20px 70px #0005}h1{font-size:clamp(26px,4vw,34px);margin:0 0 8px}.muted{color:#9aacab;margin:0 0 22px}label{display:block;margin:14px 0 7px;color:#bfd1ce}input,textarea,button{font:inherit;border-radius:9px;padding:11px 13px}input:not([type=checkbox]),textarea{background:#0b1315;border:1px solid #34484a;color:#e7f0ef}input[type=checkbox]{appearance:auto;-webkit-appearance:checkbox;display:inline-block;width:18px;height:18px;min-width:18px;margin:0 7px 0 0;padding:0;vertical-align:middle;accent-color:#52d7ca;cursor:pointer}textarea{resize:vertical;min-height:130px}button{margin:8px 6px 8px 0;border:0;background:#52d7ca;color:#06201e;font-weight:700;cursor:pointer}button:hover{background:#76e6da}.result{white-space:pre-wrap;overflow-wrap:anywhere;color:#9ce6d9;margin-top:18px}.nav{display:flex;gap:18px;margin-top:22px;color:#8da5a2;font-size:13px}.nav a{color:inherit;text-decoration:none}.nav a:hover{color:#52d7ca}.tools{display:grid;grid-template-columns:1fr 1fr;gap:10px}.table-wrap{overflow:auto;margin-top:22px}table{width:100%;border-collapse:collapse;min-width:720px}th,td{text-align:left;padding:11px 9px;border-bottom:1px solid #28383a}th{color:#9aacab;font-size:13px}th[data-sort]{cursor:pointer;white-space:nowrap}th[data-sort]:hover{color:#52d7ca}td{font-size:14px}td code{color:#9ce6d9;overflow-wrap:anywhere}.delete{background:#873d42;color:white}.usage-summary{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px;margin:20px 0}.usage-summary div{display:flex;align-items:baseline;gap:9px;padding:16px;background:#142124;border:1px solid #28383a;border-radius:12px}.usage-summary span{color:#9aacab}.usage-summary strong{font-size:24px;color:#52d7ca}.usage-summary small{color:#9aacab}.toolbar{display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap;margin-top:20px}.toolbar button{margin:0}.status{display:inline-block;padding:3px 9px;border:1px solid #477d70;border-radius:999px;color:#8ee3c2;font-size:12px}form[action="/redeem"] textarea{display:block;width:100%;min-height:150px}form[action="/redeem"] button{display:block;min-width:150px;margin:14px 0 0}form[action="/admin"]:has(input[name="password"]){max-width:420px}form[action="/admin"]:has(input[name="password"]) input[name="password"]{display:block;width:100%}form[action="/admin"]:has(input[name="password"]) button{display:block;width:100%;margin:14px 0 0}.success-head{display:flex;align-items:flex-start;gap:14px;margin-bottom:24px}.check{width:42px;height:42px;display:grid;place-items:center;border-radius:13px;background:#123c35;color:#70e0bd;font-size:24px;flex:none}.success-head h2{margin:0;font-size:25px}.success-head p{color:#9aacab;margin:4px 0 0}.address-list{border:1px solid #34484a;border-radius:12px;overflow:hidden}.address-row{display:grid;grid-template-columns:160px 1fr 72px;align-items:center;gap:12px;padding:14px 16px;background:#142124;border-bottom:1px solid #34484a}.address-row:last-child{border-bottom:0}.address-row span{color:#a9c1bd}.address-row strong{font:600 17px ui-monospace,SFMono-Regular,Consolas,monospace;color:#9ce6d9;overflow-wrap:anywhere}.copy{border:1px solid #34484a;background:#1b2b2e;color:#9ce6d9;padding:7px 10px;margin:0;font-size:13px}.copy-all{display:block;width:100%;margin:14px 0 0;padding:14px;background:#172527;color:#b9d4d0;border:1px solid #34484a}.admin-head{display:flex;align-items:center;justify-content:space-between;gap:16px}.admin-head form{margin:0}.admin-head button{margin:0;background:#1b2b2e;color:#b9d4d0;border:1px solid #34484a}.result-card{border-color:#28615b}@media(max-width:520px){.usage-summary{grid-template-columns:1fr}.tools{grid-template-columns:1fr}.address-row{grid-template-columns:110px 1fr 62px;padding:12px 10px}.address-row strong{font-size:14px}.admin-head{align-items:flex-start}}
</style>'''


def page(mode="redeem", message=""):
    if mode == "login":
        content = f'''<h1>管理员登录</h1><p class="muted">登录后管理卡密和兑换实例。</p><form method="post" action="/admin"><label>管理员密码</label><input name="password" type="password" required autocomplete="current-password"><button>登录</button></form><div class="result">{html.escape(message)}</div>'''
    elif mode == "admin":
        rows = db.execute("select code,status,port,days,expires_at,radar_password from licenses order by rowid desc limit 500").fetchall()
        usage_counts = dict(db.execute("select status,count(*) from licenses group by status").fetchall())
        used_count = usage_counts.get("used", 0)
        unused_count = usage_counts.get("new", 0)
        status_names = {"used": "已使用", "expired": "已到期", "creating": "开通中", "new": "未使用"}
        table_rows_list = []
        for code, status, port, days, expires, radar_password in rows:
            container_name = radar_container_for_port(port) if port else None
            shown_name = container_name or ("端口未匹配" if port else "—")
            table_rows_list.append(f'''<tr><td><input class="license-check" type="checkbox" name="codes" value="{html.escape(code)}" form="manage" aria-label="选择卡密"></td><td><code>{html.escape(code)}</code></td><td>{days} 天</td><td><span class="status">{status_names.get(status, status)}</span></td><td>{html.escape(shown_name)}</td><td>{(PUBLIC_IP + ':' + str(port)) if port else '—'}</td><td>{html.escape(format_expiry(expires))}</td><td>{html.escape(radar_password or '—')}</td><td><button type="submit" class="delete" form="delete-{html.escape(code)}">删除</button><form id="delete-{html.escape(code)}" method="post" action="/admin"><input type="hidden" name="action" value="delete"><input type="hidden" name="code" value="{html.escape(code)}"></form></td></tr>''')
        table_rows = "".join(table_rows_list)
        content = f'''<div class="admin-head"><h1>卡密管理</h1><form method="post" action="/logout"><button>退出登录</button></form></div><p class="muted">管理卡密、套餐时长和对应雷达实例。</p>
<form method="post" action="/admin"><input type="hidden" name="action" value="generate"><label>生成数量与有效时长</label><div class="tools"><input name="count" type="number" min="1" max="100" value="1" aria-label="生成数量"><input name="days" type="number" min="1" max="3650" value="30" aria-label="有效天数"></div><button>生成卡密</button></form><div class="result">{html.escape(message)}</div>
<div class="usage-summary"><div><span>已使用</span><strong>{used_count}</strong><small>张卡密</small></div><div><span>未使用</span><strong>{unused_count}</strong><small>张卡密</small></div><div><span>卡密总数</span><strong>{sum(usage_counts.values())}</strong><small>张卡密</small></div></div><h2>卡密列表 <span class="muted">最近 {len(rows)} 张</span></h2><form id="manage" method="post" action="/admin"><input type="hidden" name="action" value="delete_selected"></form><div class="toolbar"><label style="margin:0"><input id="select-all" type="checkbox"> 全选本页</label><div><button type="button" onclick="location.href='/admin'">刷新列表</button><button class="delete" type="submit" form="manage">删除所选</button></div></div><div class="table-wrap"><table id="license-table"><thead><tr><th>选择</th><th data-sort="1">卡密 ⇅</th><th data-sort="2">时长 ⇅</th><th data-sort="3">状态 ⇅</th><th data-sort="4">雷达容器 ⇅</th><th data-sort="5">连接地址 ⇅</th><th data-sort="6">到期时间 ⇅</th><th>pwd 密码</th><th>操作</th></tr></thead><tbody>{table_rows or '<tr><td colspan="9">暂无卡密</td></tr>'}</tbody></table></div><script>const all=document.getElementById('select-all');const boxes=()=>[...document.querySelectorAll('.license-check')];all.addEventListener('change',()=>{{boxes().forEach(box=>box.checked=all.checked);all.indeterminate=false}});boxes().forEach(box=>box.addEventListener('change',()=>{{all.checked=boxes().length>0&&boxes().every(item=>item.checked);all.indeterminate=boxes().some(item=>item.checked)&&!all.checked}}));document.querySelectorAll('#license-table th[data-sort]').forEach(th=>th.addEventListener('click',()=>{{const body=document.querySelector('#license-table tbody');const rows=[...body.querySelectorAll('tr')];const column=Number(th.dataset.sort);const direction=th.dataset.direction==='asc'?'desc':'asc';th.dataset.direction=direction;rows.sort((a,b)=>{{const left=a.cells[column].innerText.trim().toLocaleLowerCase();const right=b.cells[column].innerText.trim().toLocaleLowerCase();return (left.localeCompare(right,undefined,{{numeric:true,sensitivity:'base'}}))*(direction==='asc'?1:-1)}});rows.forEach(row=>body.appendChild(row));document.querySelectorAll('#license-table th[data-sort]').forEach(item=>{{if(item!==th){{delete item.dataset.direction}}}})}}));</script>'''
    else:
        if message.startswith("SUCCESS:"):
            entries = [line.split("|", 3) for line in message[8:].splitlines() if "|" in line]
            rows_list = []
            all_values = []
            for parts in entries:
                _, connection, radar_password, *extra = parts
                connection = connection.strip()
                host, separator, port = connection.rpartition(":")
                if not separator:
                    host, port = connection, ""
                for label, item in (("服务器地址（IP）", host), ("端口（PORT）", port), ("雷达地址（IP:PORT）", connection), ("pwd 密码", radar_password)):
                    rows_list.append(f'''<div class="address-row"><span>{label}</span><strong>{html.escape(item)}</strong><button class="copy" type="button" aria-label="复制{label}" data-copy="{html.escape(item)}">复制</button></div>''')
                all_values.append(f"{connection} pwd={radar_password}")
            rows = "".join(rows_list)
            copy_all_values = "\n".join(all_values)
            content = f'''<div class="success-head"><div class="check">✓</div><div><h2>雷达已开通</h2><p>请保存连接信息，离开页面后可凭卡密再次查看。</p></div></div><div class="address-list">{rows}</div><button class="copy-all" type="button" data-copy="{html.escape(copy_all_values)}">复制全部连接信息</button><div class="result">{html.escape(chr(10).join(parts[0] for parts in entries))}</div><p><a href="/">兑换另一张卡密</a></p><script>document.querySelectorAll('[data-copy]').forEach(b=>b.onclick=async()=>{{await navigator.clipboard.writeText(b.dataset.copy);b.textContent='已复制'}})</script>'''
        else:
            content = f'''<h1>连接共享雷达</h1><p class="muted">输入卡密即可开通雷达接入地址。支持多张卡密，每行一张。</p>
<form method="post" action="/redeem"><label>兑换卡密</label><textarea name="codes" placeholder="请粘贴卡密，每行一张" required></textarea><button>验证并开通</button></form><div class="result">{html.escape(message)}</div>'''
    nav = '<nav class="nav"><a href="/">客户兑换</a></nav>' if mode in ("admin", "login") else ""
    return f'''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>DQ 雷达</title>{STYLE}<main class="wrap"><header class="top"><span class="mark"></span><div><div class="brand">DQ RADAR</div><div class="sub">共享雷达接入服务</div></div></header><section class="card">{content}</section>{nav}</main></html>'''


class Handler(BaseHTTPRequestHandler):
    def session_token(self):
        cookies = SimpleCookie()
        try:
            cookies.load(self.headers.get("Cookie", ""))
        except Exception:
            return ""
        morsel = cookies.get("dq_session")
        return morsel.value if morsel else ""

    def send_page(self, content, status=200):
        body = content.encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith("/result/"):
            token = self.path.removeprefix("/result/")
            return self.send_page(REDEEM_RESULTS.get(token, page("redeem", "兑换结果已失效，请重新查询卡密状态。")))
        if self.path == "/admin":
            token = self.session_token()
            with SESSION_LOCK:
                authenticated = token in ADMIN_SESSIONS
            return self.send_page(page("admin" if authenticated else "login"))
        self.send_page(page())

    def do_POST(self):
        form = parse_qs(self.rfile.read(int(self.headers.get("Content-Length", "0"))).decode())
        if self.path == "/admin":
            token = self.session_token()
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
                self.send_header("Set-Cookie", f"dq_session={token}; HttpOnly; SameSite=Lax; Path=/admin; Max-Age=28800")
                self.end_headers()
                return
            action = form.get("action", [""])[0]
            if action == "delete":
                codes = form.get("code", [])
                with db:
                    for code in codes:
                        row = db.execute("select port from licenses where code=?", (code,)).fetchone()
                        if row and row[0]:
                            container_name = radar_container_for_port(row[0])
                            if container_name:
                                delete_radar(container_name)
                        db.execute("delete from licenses where code=?", (code,))
                return self.send_page(page("admin", f"已删除 {len(codes)} 张卡密及其关联实例。"))
            if action == "delete_selected":
                codes = form.get("codes", [])
                with db:
                    for code in codes:
                        row = db.execute("select port from licenses where code=?", (code,)).fetchone()
                        if row and row[0]:
                            container_name = radar_container_for_port(row[0])
                            if container_name:
                                delete_radar(container_name)
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
            token = self.session_token()
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
                    row = db.execute("select status,port,days,expires_at,radar_password from licenses where code=?", (code,)).fetchone()
                    if row is None:
                        results.append(f"{code}: 卡密无效")
                        continue
                    if row[0] == "used":
                        results.append(f"{code}|{PUBLIC_IP}:{row[1]}|{row[4] or ''}")
                        continue
                    port = next_radar_slot()
                    radar_password = f"{secrets.randbelow(10000):04d}"
                    db.execute("update licenses set status='creating',port=?,radar_password=? where code=?", (port, radar_password, code))
                name = f"nexoradar-{secrets.token_hex(6)}"
                try:
                    create_radar(name, port, radar_password)
                    expiry = (datetime.now(timezone.utc) + timedelta(days=row[2])).isoformat(timespec="seconds")
                    with db:
                        db.execute("update licenses set status='used',expires_at=? where code=?", (expiry, code))
                    results.append(f"{code}|{PUBLIC_IP}:{port}|{radar_password}|{row[2]}")
                except (RuntimeError, OSError, ValueError) as error:
                    logging.exception("Radar container creation failed for license %s on port %s", code, port)
                    with db:
                        db.execute("update licenses set status='new',port=null,radar_password=null where code=?", (code,))
                    results.append(f"{code}: 创建失败。请联系管理员并提供端口 {port} 和时间。")
            successes = [line for line in results if "|" in line]
            failures = [line for line in results if "|" not in line]
            if successes:
                result_page = page("redeem", "SUCCESS:" + "\n".join(successes))
                if failures:
                    result_page = result_page.replace('<p><a href="/">兑换另一张卡密</a></p>', f'<div class="result">{html.escape(chr(10).join(failures))}</div><p><a href="/">兑换另一张卡密</a></p>')
            else:
                result_page = page("redeem", "\n".join(failures))
            token = secrets.token_urlsafe(24)
            REDEEM_RESULTS[token] = result_page
            self.send_response(303)
            self.send_header("Location", f"/result/{token}")
            self.end_headers()
            return
        self.send_page("Not found", 404)


if __name__ == "__main__":
    threading.Thread(target=expire_rentals, daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
