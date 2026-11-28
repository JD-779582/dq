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


def radar_network_totals():
    connection = DockerConnection()
    connection.request("GET", "/v1.44/containers/json?all=0")
    response = connection.getresponse()
    data = response.read()
    connection.close()
    if response.status != 200:
        raise RuntimeError(data.decode(errors="replace"))
    result = {}
    for container in json.loads(data):
        ports = [item.get("PublicPort") for item in container.get("Ports", [])]
        port = next((item for item in ports if item), None)
        if port is not None:
            result[str(port)] = {"id": container["Id"], "rx": 0, "tx": 0}
    for port, item in result.items():
        connection = DockerConnection()
        connection.request("GET", f"/v1.44/containers/{item['id']}/stats?stream=false&one-shot=true")
        response = connection.getresponse()
        data = response.read()
        connection.close()
        if response.status != 200:
            continue
        stats = json.loads(data)
        for network in stats.get("networks", {}).values():
            item["rx"] += network.get("rx_bytes", 0)
            item["tx"] += network.get("tx_bytes", 0)
    return result


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


STYLE = """<style>
:root {
  color-scheme: dark; --bg: #141415; --surface: #19191b; --raised: #232326;
  --line: #303033; --ink: #ededed; --secondary: #babac0; --muted: #98989f;
  --signal: #91d3ba; --danger: #efaaa4;
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--ink); font: 15px/1.55 system-ui, "Microsoft YaHei", sans-serif; }
h1,h2,p { margin: 0; }
h1 { font-size: clamp(24px,4vw,32px); font-weight: 600; line-height: 1.25; }
h2 { font-size: 18px; font-weight: 600; }
p,.muted,label { color: var(--secondary); }
.top { border-bottom: 1px solid var(--line); }
.mast { width: min(100% - 48px,1440px); min-height: 60px; margin: auto; display: flex; align-items: center; gap: 12px; }
.mark { width: 16px; height: 16px; border: 1px solid var(--signal); border-radius: 50%; display: grid; place-items: center; }
.mark:before { content: ""; width: 4px; height: 4px; border-radius: 50%; background: var(--signal); }
.brand { font-weight: 600; }
.sub { margin-inline-start: auto; color: var(--muted); font-size: 13px; }
.wrap { width: min(100% - 48px,1440px); margin: 32px auto; }
.wrap.redeem { max-width: 640px; margin-top: 64px; }
.wrap.login { max-width: 384px; margin-top: 64px; }
.login h1,.redeem h1 { margin-bottom: 8px; }
.login label,.redeem-form label { display: block; margin-bottom: 8px; }
.login form,.redeem-form { margin-top: 24px; }
input,textarea,select,button { font: inherit; color: inherit; }
input:not([type=checkbox]),textarea,select,button { min-height: 40px; padding: 8px 12px; border: 1px solid var(--line); border-radius: 8px; background: var(--surface); }
input[type=checkbox] { width: 16px; height: 16px; margin: 0; accent-color: var(--signal); vertical-align: middle; }
.login input,textarea { display: block; width: 100%; }
textarea { min-height: 144px; resize: vertical; }
button { cursor: pointer; white-space: nowrap; }
button:hover:not(:disabled) { background: var(--raised); }
button:disabled { opacity: .5; cursor: default; }
:focus-visible { outline: 2px solid var(--signal); outline-offset: 2px; }
.login button,.redeem-form button,.generate-box button { background: var(--ink); color: var(--bg); }
.login button:hover,.redeem-form button:hover,.generate-box button:hover { background: #d7d7da; }
.login button,.redeem-form button { margin-top: 16px; }
.login button { width: 100%; }
.admin-head,.toolbar,.list-tools,.tools,.generate-box { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; }
.admin-head,.toolbar { justify-content: space-between; }
.admin-head p,.section-heading p { margin-top: 4px; font-size: 13px; }
.section-heading { margin: 24px 0 8px; }
.generate-box { align-items: end; gap: 12px; }
.tools input { display: block; width: 112px; margin-top: 4px; }
.usage-summary { display: flex; flex-wrap: wrap; gap: 12px 32px; margin: 24px 0; padding: 12px 0; border-block: 1px solid var(--line); }
.usage-summary div { display: flex; align-items: baseline; gap: 8px; color: var(--secondary); font-size: 13px; }
.usage-summary strong { font-size: 20px; color: var(--ink); font-weight: 500; font-variant-numeric: tabular-nums; }
.usage-summary small { display: none; }
.toolbar { margin: 12px 0; }
.toolbar label { display: inline-flex; align-items: center; gap: 8px; }
.list-tools input { width: 240px; max-width: 100%; }
.list-tools button { flex: none; }
.table-wrap { overflow: auto; border-block: 1px solid var(--line); }
table { width: 100%; min-width: 1000px; border-collapse: collapse; text-align: start; }
th,td { text-align: start; padding: 8px 12px; border-bottom: 1px solid var(--line); font-size: 13px; }
th { background: var(--surface); color: var(--secondary); font-weight: 500; white-space: nowrap; }
th button { min-height: 32px; padding: 4px; margin: 0 -4px; background: transparent; border-color: transparent; }
th[aria-sort] button,.status-used { color: var(--signal); }
tbody tr:last-child td { border-bottom: 0; }
tbody tr:hover,tbody tr:focus-within { background: var(--surface); }
tbody tr:has(.license-check:checked) { background: var(--raised); }
td code,.endpoint,.traffic,.address-row strong { font-family: ui-monospace, Consolas, monospace; font-variant-numeric: tabular-nums; }
.traffic { white-space: nowrap; color: var(--signal); }
.status { white-space: nowrap; }
button.delete { color: var(--danger); }
.result { margin-top: 16px; color: var(--secondary); white-space: pre-wrap; overflow-wrap: anywhere; }
.result:empty { display: none; }
.nav { margin-top: 20px; }
a { color: var(--secondary); text-underline-offset: 4px; }
.success-head { margin-bottom: 20px; }
.success-head p { margin-top: 8px; }
.address-list { border-block: 1px solid var(--line); }
.address-row { display: grid; grid-template-columns: 112px minmax(0,1fr) auto; align-items: center; gap: 12px; padding: 8px 0; border-bottom: 1px solid var(--line); }
.address-row:last-child { border-bottom: 0; }
.address-row span { color: var(--muted); }
.address-row strong { overflow-wrap: anywhere; font-weight: 500; }
.address-row:nth-child(3n) strong { color: var(--signal); }
.copy-all { margin: 16px 0; }
.redeem-layout { display:grid; grid-template-columns:minmax(220px,.8fr) minmax(320px,1.2fr); gap:36px; align-items:center; }
.redeem-story { max-width:330px; }
.redeem-story h1 { font-size:32px; }
.redeem-story p { max-width:30ch; margin:12px 0 0; }
.radar-mark { position:relative; display:grid; width:136px; aspect-ratio:1; place-items:center; margin:28px 0 0; border:1px solid var(--line); border-radius:50%; background:repeating-linear-gradient(0deg,transparent 0 33px,#303033 34px,#303033 35px,transparent 36px),repeating-linear-gradient(90deg,transparent 0 33px,#303033 34px,#303033 35px,transparent 36px); }
.radar-mark:before,.radar-mark:after { position:absolute; content:""; border:1px solid #45454a; border-radius:50%; }
.radar-mark:before { inset:20px; }
.radar-mark:after { inset:43px; border-color:var(--accent); }
.radar-mark span { z-index:1; width:6px; height:6px; border-radius:50%; background:var(--accent); }
.radar-caption { display:block; margin-top:10px; color:var(--muted); font:11px ui-monospace,Consolas,monospace; }
.redeem-form { padding:20px; background:var(--surface); border:1px solid var(--line); border-radius:10px; }
@media (max-width: 600px) {
  .mast,.wrap { width: calc(100% - 32px); }
  .wrap.redeem,.wrap.login { margin-top: 40px; }
  .redeem-layout { grid-template-columns:1fr; gap:20px; }
  .redeem-story { max-width:none; }
  .redeem-story h1 { font-size:27px; }
  .radar-mark { width:76px; margin-top:16px; }
  .radar-caption { margin-top:6px; }
  .list-tools { width: 100%; }
  .list-tools input { width: 100%; }
  .address-row { grid-template-columns: minmax(0,1fr) auto; gap: 4px 8px; }
  .address-row strong { grid-row: 2; }
  .address-row button { grid-area: 1 / 2 / span 2; }
}
</style>"""


def page(mode="redeem", message=""):
    if mode == "login":
        content = f'''<h1>管理员登录</h1><p class="muted">查看卡密、雷达实例和实时流量</p><form method="post" action="/admin"><label for="admin-password">管理员密码</label><input id="admin-password" name="password" type="password" required autocomplete="current-password"><button>登录</button></form><div class="result">{html.escape(message)}</div>'''
    elif mode == "admin":
        rows = db.execute("select code,status,port,days,expires_at from licenses order by rowid desc limit 500").fetchall()
        usage_counts = dict(db.execute("select status,count(*) from licenses group by status").fetchall())
        used_count = usage_counts.get("used", 0)
        unused_count = usage_counts.get("new", 0)
        status_names = {"used": "已使用", "expired": "已到期", "creating": "开通中", "new": "未使用"}
        table_rows_list = []
        for code, status, port, days, expires in rows:
            container_name = radar_container_for_port(port) if port else None
            shown_name = container_name or ("端口未匹配" if port else "—")
            traffic_cell = f'<span class="traffic" data-port="{port}">上传 —<br>下载 —</span>' if port and status == "used" else "—"
            table_rows_list.append(f'''<tr><td><input class="license-check" type="checkbox" name="codes" value="{html.escape(code)}" form="manage" aria-label="选择卡密"></td><td><code>{html.escape(code)}</code></td><td>{days} 天</td><td><span class="status status-{html.escape(status)}">{status_names.get(status, status)}</span></td><td>{html.escape(shown_name)}</td><td class="endpoint">{html.escape(PUBLIC_IP + ':' + str(port)) if port else '—'}</td><td>{html.escape(format_expiry(expires))}</td><td>{traffic_cell}</td><td><button type="submit" class="delete" form="delete-{html.escape(code)}">删除</button><form id="delete-{html.escape(code)}" method="post" action="/admin"><input type="hidden" name="action" value="delete"><input type="hidden" name="code" value="{html.escape(code)}"></form></td></tr>''')
        table_rows = "".join(table_rows_list)
        content = f'''<div class="admin-head"><div><h1>卡密管理</h1><p class="muted">管理卡密、租期和容器流量</p></div><form method="post" action="/logout"><button>退出登录</button></form></div>
<div class="section-heading"><div><h2>生成卡密</h2></div></div><form class="generate-box" method="post" action="/admin"><input type="hidden" name="action" value="generate"><div class="tools"><label>生成数量<input name="count" type="number" min="1" max="100" value="1" aria-label="生成数量"></label><label>有效天数<input name="days" type="number" min="1" max="3650" value="30" aria-label="有效天数"></label></div><button>生成卡密</button></form><div class="result">{html.escape(message)}</div>
<div class="usage-summary"><div><span>已使用</span><strong>{used_count}</strong><small>张</small></div><div><span>未使用</span><strong>{unused_count}</strong><small>张</small></div><div><span>全部卡密</span><strong>{sum(usage_counts.values())}</strong><small>张</small></div></div><div class="section-heading"><div><h2>卡密与雷达实例</h2><p>显示最近 {len(rows)} 张卡密，流量每 3 秒更新。</p></div></div><form id="manage" method="post" action="/admin"><input type="hidden" name="action" value="delete_selected"></form><div class="toolbar"><label style="margin:0"><input id="select-all" type="checkbox"> 全选本页</label><div class="list-tools"><input id="license-search" type="search" aria-label="搜索卡密、地址或容器" placeholder="搜索卡密、地址或容器"><select id="status-filter" aria-label="筛选卡密状态"><option value="">全部状态</option><option value="已使用">已使用</option><option value="未使用">未使用</option><option value="已到期">已到期</option><option value="开通中">开通中</option></select><button type="button" onclick="location.href='/admin'">刷新</button><button class="delete" type="submit" form="manage">删除所选</button></div></div><div class="table-wrap"><table id="license-table"><thead><tr><th>选择</th><th><button type="button" data-sort="1">卡密 <span aria-hidden="true">↕</span></button></th><th><button type="button" data-sort="2">时长 <span aria-hidden="true">↕</span></button></th><th><button type="button" data-sort="3">状态 <span aria-hidden="true">↕</span></button></th><th><button type="button" data-sort="4">雷达容器 <span aria-hidden="true">↕</span></button></th><th><button type="button" data-sort="5">连接地址 <span aria-hidden="true">↕</span></button></th><th><button type="button" data-sort="6">到期时间 <span aria-hidden="true">↕</span></button></th><th>上传/下载</th><th>操作</th></tr></thead><tbody>{table_rows or '<tr><td colspan="9">暂无卡密</td></tr>'}</tbody></table></div><script>const all=document.getElementById('select-all');const boxes=()=>[...document.querySelectorAll('.license-check')].filter(box=>!box.closest('tr').hidden);all.addEventListener('change',()=>{{boxes().forEach(box=>box.checked=all.checked);all.indeterminate=false}});function syncSelection(){{const visible=boxes();const count=visible.filter(box=>box.checked).length;all.checked=count>0&&count===visible.length;all.indeterminate=count>0&&!all.checked;all.disabled=!visible.length;document.querySelector('button[form=manage]').disabled=!count}}document.querySelectorAll('.license-check').forEach(box=>box.addEventListener('change',syncSelection));all.addEventListener('change',syncSelection);syncSelection();const search=document.getElementById('license-search'),filter=document.getElementById('status-filter');function filterRows(){{document.querySelectorAll('#license-table tbody tr').forEach(row=>{{const text=row.textContent.toLocaleLowerCase();const query=search.value.trim().toLocaleLowerCase();const status=row.cells[3]?.textContent.trim()||'';row.hidden=(query&&!text.includes(query))||(filter.value&&status!==filter.value);if(row.hidden){{const box=row.querySelector('.license-check');if(box)box.checked=false}}}});syncSelection()}}search.addEventListener('input',filterRows);filter.addEventListener('change',filterRows);document.querySelectorAll('#license-table button[data-sort]').forEach(th=>th.addEventListener('click',()=>{{const body=document.querySelector('#license-table tbody');const rows=[...body.rows].filter(row=>row.cells.length>1);const column=Number(th.dataset.sort);const direction=th.dataset.direction==='asc'?'desc':'asc';th.dataset.direction=direction;rows.sort((a,b)=>{{const left=a.cells[column].innerText.trim().toLocaleLowerCase();const right=b.cells[column].innerText.trim().toLocaleLowerCase();return left.localeCompare(right,undefined,{{numeric:true,sensitivity:'base'}})*(direction==='asc'?1:-1)}});rows.forEach(row=>body.appendChild(row));document.querySelectorAll('#license-table button[data-sort]').forEach(item=>{{item.closest('th').removeAttribute('aria-sort');item.querySelector('span').textContent='↕';if(item!==th)delete item.dataset.direction}});th.closest('th').setAttribute('aria-sort',direction==='asc'?'ascending':'descending');th.querySelector('span').textContent=direction==='asc'?'↑':'↓';filterRows()}}));async function refreshTraffic(){{const cells=document.querySelectorAll('.traffic');if(!cells.length)return;try{{const response=await fetch('/admin/traffic',{{cache:'no-store',credentials:'same-origin'}});if(!response.ok){{cells.forEach(cell=>cell.textContent='监控 HTTP '+response.status);return}}const current=await response.json(),now=Date.now();cells.forEach(cell=>{{const item=current[cell.dataset.port];if(!item){{cell.textContent='未匹配端口';return}}const old=window.trafficPrevious?.[cell.dataset.port];if(old){{const seconds=(now-old.time)/1000;cell.innerHTML='↑ '+formatRate(Math.max(0,item.tx-old.tx)/seconds)+'<br>↓ '+formatRate(Math.max(0,item.rx-old.rx)/seconds)}}else cell.textContent='采样中…'}});window.trafficPrevious=Object.fromEntries(Object.entries(current).map(([port,item])=>[port,{{...item,time:now}}]))}}catch(e){{cells.forEach(cell=>cell.textContent='监控请求失败')}}}}function formatRate(value){{const units=['B/s','KB/s','MB/s','GB/s'];let i=0;while(value>=1024&&i<units.length-1){{value/=1024;i++}}return value.toFixed(i?1:0)+' '+units[i]}}refreshTraffic();setInterval(refreshTraffic,3000);</script>'''
    else:
        if message.startswith("SUCCESS:"):
            entries = [line.split("|", 1) for line in message[8:].splitlines() if "|" in line]
            rows_list = []
            all_values = []
            for _, connection in entries:
                connection = connection.split('（', 1)[0].strip()
                host, separator, port = connection.rpartition(":")
                if not separator:
                    host, port = connection, ""
                for label, item in (("服务器地址", host), ("端口", port), ("雷达地址", connection)):
                    rows_list.append(f'''<div class="address-row"><span>{label}</span><strong>{html.escape(item)}</strong><button class="copy" type="button" aria-label="复制{label}" data-copy="{html.escape(item)}">复制</button></div>''')
                all_values.append(connection)
            rows = "".join(rows_list)
            copy_all_values = "\n".join(all_values)
            content = f'''<div class="success-head"><div><h1>雷达已开通</h1><p>可凭原卡密再次查看连接地址</p></div></div><div class="address-list">{rows}</div><button class="copy-all" type="button" data-copy="{html.escape(copy_all_values)}">复制全部连接信息</button><div class="result">{html.escape(chr(10).join(code for code, _ in entries))}</div><p><a href="/">兑换另一张卡密</a></p><script>document.querySelectorAll('[data-copy]').forEach(b=>b.onclick=async()=>{{await navigator.clipboard.writeText(b.dataset.copy);b.textContent='已复制'}})</script>'''
        else:
            content = f'''<div class="redeem-layout"><section class="redeem-story"><h1>共享雷达接入</h1><p>兑换卡密后获取雷达连接地址。一次可提交多张，每行一张。</p><div class="radar-mark" aria-hidden="true"><span></span></div><small class="radar-caption">DQ / RADAR ACCESS</small></section><section><form class="redeem-form" method="post" action="/redeem"><label for="redeem-codes">卡密</label><textarea id="redeem-codes" name="codes" placeholder="粘贴卡密，每行一张" required></textarea><button>验证并开通</button></form><div class="result">{html.escape(message)}</div></section></div>'''
    nav = '<nav class="nav"><a href="/">客户兑换</a></nav>' if mode in ("admin", "login") else ""
    return f'''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>DQ 雷达</title>{STYLE}<header class="top"><div class="mast"><span class="mark" aria-hidden="true"></span><span class="brand">DQ 雷达</span><span class="sub">{"运营管理" if mode in ("admin", "login") else "客户接入"}</span></div></header><main class="wrap {mode}">{content}{nav}</main></html>'''


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
        if self.path == "/admin/traffic":
            with SESSION_LOCK:
                authenticated = self.session_token() in ADMIN_SESSIONS
            if not authenticated:
                self.send_error(403)
                return
            try:
                payload = json.dumps(radar_network_totals()).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
            except (OSError, RuntimeError, ValueError) as error:
                logging.exception("Failed to collect container traffic")
                self.send_error(503, str(error))
            return
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
                    row = db.execute("select status,port,days,expires_at from licenses where code=?", (code,)).fetchone()
                    if row is None:
                        results.append(f"{code}: 卡密无效")
                        continue
                    if row[0] == "used":
                        results.append(f"{code}|{PUBLIC_IP}:{row[1]}")
                        continue
                    port = next_radar_slot()
                    db.execute("update licenses set status='creating',port=? where code=?", (port, code))
                name = f"nexoradar-{port}"
                try:
                    create_radar(name, port)
                    expiry = (datetime.now(timezone.utc) + timedelta(days=row[2])).isoformat(timespec="seconds")
                    with db:
                        db.execute("update licenses set status='used',expires_at=? where code=?", (expiry, code))
                    results.append(f"{code}|{PUBLIC_IP}:{port}（有效 {row[2]} 天）")
                except (RuntimeError, OSError, ValueError) as error:
                    logging.exception("Radar container creation failed for license %s on port %s", code, port)
                    with db:
                        db.execute("update licenses set status='new',port=null where code=?", (code,))
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
