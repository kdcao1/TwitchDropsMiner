from __future__ import annotations

import hmac
import asyncio
import logging
from typing import Any, TYPE_CHECKING

from aiohttp import web

from constants import State, PriorityMode

if TYPE_CHECKING:
    from twitch import Twitch
    from channel import Channel
    from inventory import DropsCampaign, TimedDrop


logger = logging.getLogger("TwitchDrops")


def _drop_json(drop: TimedDrop) -> dict[str, Any]:
    return {
        "id": drop.id,
        "name": drop.rewards_text() or drop.name,
        "current": drop.current_minutes,
        "required": drop.required_minutes,
        "progress": drop.progress,
        "claimed": drop.is_claimed,
        "ends_at": drop.ends_at.isoformat(),
    }


def _channel_json(channel: Channel) -> dict[str, Any]:
    return {
        "id": channel.id,
        "name": channel.name,
        "game": channel.game.name if channel.game is not None else None,
        "viewers": channel.viewers,
        "online": channel.online,
        "pending": channel.pending_online,
        "drops": channel.drops_enabled,
        "acl": channel.acl_based,
        "url": str(channel.url),
    }


class WebUI:
    def __init__(self, twitch: Twitch):
        self._twitch = twitch
        self._runner: web.AppRunner | None = None

    @property
    def configured(self) -> bool:
        settings = self._twitch.settings
        return bool(settings.webui_enabled and settings.webui_password)

    async def start(self) -> None:
        if self._runner is not None or not self.configured:
            return
        settings = self._twitch.settings
        app = web.Application(middlewares=[self._auth])
        app.add_routes([
            web.get("/", self._index),
            web.get("/api/state", self._state),
            web.post("/api/settings", self._settings),
            web.post("/api/reload", self._reload),
            web.post("/api/switch", self._switch),
        ])
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        try:
            await web.TCPSite(runner, settings.webui_host, settings.webui_port).start()
        except OSError as exc:
            await runner.cleanup()
            self._twitch.print(f"Web UI: failed to start: {exc}")
            return
        self._runner = runner
        self._twitch.print(
            f"Web UI: listening on http://{settings.webui_host}:{settings.webui_port}"
        )

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None

    async def restart(self) -> None:
        await self.stop()
        await self.start()

    @web.middleware
    async def _auth(self, request: web.Request, handler: Any) -> web.StreamResponse:
        if request.path.startswith("/api/"):
            token = request.headers.get("Authorization", "").removeprefix("Bearer ")
            expected = self._twitch.settings.webui_password
            if not hmac.compare_digest(token.encode(), expected.encode()):
                await asyncio.sleep(1)
                raise web.HTTPUnauthorized()
        return await handler(request)

    async def _index(self, request: web.Request) -> web.Response:
        return web.Response(text=PAGE, content_type="text/html")

    def _campaign_json(self, campaign: DropsCampaign) -> dict[str, Any]:
        status = "active" if campaign.active else "upcoming" if campaign.upcoming else "expired"
        return {
            "id": campaign.id,
            "name": campaign.name,
            "game": campaign.game.name,
            "status": status,
            "starts_at": campaign.starts_at.isoformat(),
            "ends_at": campaign.ends_at.isoformat(),
            "linked": campaign.linked,
            "link_url": campaign.link_url,
            "eligible": campaign.eligible,
            "finished": campaign.finished,
            "can_finish": campaign.can_finish(),
            "claimed": campaign.claimed_drops,
            "total": campaign.total_drops,
            "progress": campaign.progress,
            "remaining_minutes": campaign.remaining_minutes,
            "prioritized": campaign.id in self._twitch.settings.priority_campaigns,
            "drops": [_drop_json(drop) for drop in campaign.drops],
        }

    async def _state(self, request: web.Request) -> web.Response:
        twitch = self._twitch
        settings = twitch.settings
        watching = twitch.watching_channel.get_with_default(None)
        drop = None
        if watching is not None and (campaign := twitch.get_active_campaign(watching)):
            drop = campaign.first_drop
        if drop is None:
            drop = twitch.gui.progress.current_drop
        drop_data = None
        if drop is not None:
            drop_data = _drop_json(drop) | {
                "campaign": drop.campaign.name,
                "game": drop.campaign.game.name,
                "campaign_claimed": drop.campaign.claimed_drops,
                "campaign_total": drop.campaign.total_drops,
                "campaign_progress": drop.campaign.progress,
            }
        campaigns = [c for c in twitch.inventory if not c.expired]
        campaigns.sort(key=lambda c: (
            c.id not in settings.priority_campaigns,
            not (c.progress > 0 and not c.finished),
            c.ends_at,
        ))
        return web.json_response({
            "status": twitch.gui.status.text,
            "watching": _channel_json(watching) if watching is not None else None,
            "drop": drop_data,
            "channels": [_channel_json(channel) for channel in twitch.channels.values()],
            "campaigns": [self._campaign_json(c) for c in campaigns],
            "games": sorted({c.game.name for c in twitch.inventory}),
            "settings": {
                "priority": settings.priority,
                "exclude": sorted(settings.exclude),
                "priority_mode": settings.priority_mode.name,
                "priority_campaigns": settings.priority_campaigns,
                "available_drops_check": settings.available_drops_check,
                "enable_badges_emotes": settings.enable_badges_emotes,
            },
            "priority_modes": [mode.name for mode in PriorityMode],
            "log": list(twitch.gui.history),
        })

    async def _settings(self, request: web.Request) -> web.Response:
        settings = self._twitch.settings
        data = await request.json()
        if not isinstance(data, dict):
            raise web.HTTPBadRequest()
        try:
            if "priority" in data:
                settings.priority = list(dict.fromkeys(str(g) for g in data["priority"] if g))
            if "exclude" in data:
                settings.exclude = {str(g) for g in data["exclude"] if g}
            if "priority_campaigns" in data:
                settings.priority_campaigns = list(
                    dict.fromkeys(str(c) for c in data["priority_campaigns"])
                )
            if "priority_mode" in data:
                settings.priority_mode = PriorityMode[data["priority_mode"]]
            for key in ("available_drops_check", "enable_badges_emotes"):
                if key in data:
                    setattr(settings, key, bool(data[key]))
        except (KeyError, TypeError) as exc:
            raise web.HTTPBadRequest(text=str(exc))
        settings.save()
        self._twitch.gui.settings.refresh()
        self._twitch.change_state(State.INVENTORY_FETCH)
        return web.json_response({"ok": True})

    async def _reload(self, request: web.Request) -> web.Response:
        self._twitch.change_state(State.INVENTORY_FETCH)
        return web.json_response({"ok": True})

    async def _switch(self, request: web.Request) -> web.Response:
        data = await request.json()
        try:
            channel = self._twitch.channels[int(data["channel_id"])]
        except (KeyError, TypeError, ValueError):
            raise web.HTTPNotFound()
        self._twitch.gui.channels.select(channel)
        self._twitch.change_state(State.CHANNEL_SWITCH)
        return web.json_response({"ok": True})


PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Twitch Drops Miner</title>
<style>
:root{--bg:#f6f6f8;--card:#fff;--fg:#1d1d22;--muted:#6b6b76;--line:#e2e2e8;--accent:#7c4dff;--ok:#1f9d55;--warn:#c98a00;--bad:#d64545}
@media (prefers-color-scheme:dark){:root{--bg:#121216;--card:#1c1c22;--fg:#ececf1;--muted:#9a9aa6;--line:#2c2c35;--accent:#a68bff}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.45 system-ui,-apple-system,Segoe UI,sans-serif}
header{display:flex;gap:12px;align-items:center;padding:12px 16px;border-bottom:1px solid var(--line);background:var(--card);position:sticky;top:0;z-index:1}
header h1{font-size:16px;margin:0;flex:1}
nav button{background:none;border:0;color:var(--muted);padding:6px 10px;border-radius:6px;cursor:pointer;font:inherit}
nav button.on{color:var(--fg);background:var(--bg)}
main{max-width:1000px;margin:0 auto;padding:16px}
section{display:none}section.on{display:block}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px;margin-bottom:12px}
.card h2{font-size:13px;text-transform:uppercase;letter-spacing:.04em;color:var(--muted);margin:0 0 8px}
.muted{color:var(--muted)}
.bar{height:8px;background:var(--line);border-radius:4px;overflow:hidden;margin:6px 0}
.bar>i{display:block;height:100%;background:var(--accent)}
button.b,select,input{font:inherit;color:var(--fg);background:var(--bg);border:1px solid var(--line);border-radius:6px;padding:5px 9px}
button.b{cursor:pointer}button.b:hover{border-color:var(--accent)}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:12px}
ul.list{list-style:none;margin:8px 0;padding:0}
ul.list li{display:flex;align-items:center;gap:6px;padding:4px 0;border-bottom:1px solid var(--line)}
ul.list li span{flex:1}
.row{display:flex;gap:6px;align-items:center;flex-wrap:wrap}
table{width:100%;border-collapse:collapse}
td,th{text-align:left;padding:5px 6px;border-bottom:1px solid var(--line)}
.tag{font-size:12px;padding:1px 6px;border-radius:9px;border:1px solid var(--line)}
.active{color:var(--ok)}.upcoming{color:var(--warn)}.bad{color:var(--bad)}
.drops{display:flex;gap:8px;flex-wrap:wrap;margin-top:8px}
.drop{flex:1 1 150px;border:1px solid var(--line);border-radius:8px;padding:6px 8px;font-size:13px}
pre{white-space:pre-wrap;word-break:break-word;margin:0;font-size:12px;max-height:60vh;overflow:auto}
#login{max-width:320px;margin:15vh auto}
#login input{width:100%;margin:8px 0}
</style>
</head>
<body>
<div id="login" class="card" hidden>
<h2>Twitch Drops Miner</h2>
<form id="lf"><input id="pw" type="password" placeholder="Password" autocomplete="current-password"><button class="b">Sign in</button></form>
<p id="lerr" class="bad"></p>
</div>
<div id="app" hidden>
<header><h1>Twitch Drops Miner</h1>
<nav><button data-t="main" class="on">Main</button><button data-t="inv">Campaigns</button><button data-t="set">Settings</button><button data-t="log">Log</button></nav></header>
<main>
<section id="main" class="on">
<div class="card"><h2>Status</h2><div id="status"></div></div>
<div class="card"><h2>Progress</h2><div id="drop"></div></div>
<div class="card"><h2>Channels</h2><div class="row"><button class="b" id="reload">Reload</button></div><table id="channels"></table></div>
</section>
<section id="inv"><div class="row" style="margin-bottom:12px"><input id="q" placeholder="Search campaigns" style="flex:1"></div><div id="campaigns"></div></section>
<section id="set">
<div class="grid">
<div class="card"><h2>Priority</h2><div class="row"><input id="padd" list="games" placeholder="Game name" style="flex:1"><button class="b" id="paddb">Add</button></div><ul class="list" id="prio"></ul></div>
<div class="card"><h2>Exclude</h2><div class="row"><input id="eadd" list="games" placeholder="Game name" style="flex:1"><button class="b" id="eaddb">Add</button></div><ul class="list" id="excl"></ul></div>
<div class="card"><h2>Options</h2>
<p><label>Priority mode <select id="mode"></select></label></p>
<p><label><input type="checkbox" id="avail"> Available drops check</label></p>
<p><label><input type="checkbox" id="badges"> Enable badges and emotes</label></p>
</div>
</div>
<datalist id="games"></datalist>
</section>
<section id="log"><div class="card"><pre id="logs"></pre></div></section>
</main>
</div>
<script>
const $=id=>document.getElementById(id);
const esc=s=>String(s??"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"})[c]);
const pct=p=>(Math.max(0,Math.min(1,p))*100).toFixed(1)+"%";
const mins=m=>m>=60?Math.floor(m/60)+"h "+String(m%60).padStart(2,"0")+"m":m+"m";
const when=s=>new Date(s).toLocaleString([], {month:"short",day:"numeric",hour:"2-digit",minute:"2-digit"});
let token=localStorage.getItem("tdm_token")||"",S=null,setKey="";
async function api(path,body){
  const r=await fetch(path,{method:body?"POST":"GET",headers:{"Authorization":"Bearer "+token,"Content-Type":"application/json"},body:body?JSON.stringify(body):undefined});
  if(r.status===401){showLogin("Wrong password");throw new Error("unauthorized")}
  if(!r.ok)throw new Error(r.status);
  return r.json();
}
function showLogin(err){$("app").hidden=true;$("login").hidden=false;$("lerr").textContent=err||""}
$("lf").onsubmit=e=>{e.preventDefault();token=$("pw").value;localStorage.setItem("tdm_token",token);refresh()};
document.querySelectorAll("nav button").forEach(b=>b.onclick=()=>{
  document.querySelectorAll("nav button,section").forEach(x=>x.classList.remove("on"));
  b.classList.add("on");$(b.dataset.t).classList.add("on");
});
const save=patch=>api("api/settings",patch).then(refresh);
function render(){
  $("status").textContent=S.status||"-";
  const d=S.drop,w=S.watching;
  $("drop").innerHTML=(w?`<div>Watching <a href="${esc(w.url)}" target="_blank" rel="noopener">${esc(w.name)}</a> <span class="muted">${esc(w.game||"")}${w.viewers!=null?" · "+w.viewers+" viewers":""}</span></div>`:'<div class="muted">Not watching</div>')+
    (d?`<p><b>${esc(d.name)}</b><br><span class="muted">${esc(d.campaign)} · ${esc(d.game)}</span></p>
    <div>Drop ${pct(d.progress)} · ${mins(d.current)} / ${mins(d.required)} · ${mins(d.required-d.current)} left</div><div class="bar"><i style="width:${pct(d.progress)}"></i></div>
    <div>Campaign ${d.campaign_claimed}/${d.campaign_total} · ${pct(d.campaign_progress)}</div><div class="bar"><i style="width:${pct(d.campaign_progress)}"></i></div>`:"");
  $("channels").innerHTML="<tr><th>Channel</th><th>Game</th><th>Viewers</th><th></th></tr>"+S.channels.map(c=>
    `<tr><td>${w&&w.id===c.id?"▶ ":""}${esc(c.name)}${c.acl?' <span class="tag">ACL</span>':""}</td><td>${c.online?esc(c.game||""):c.pending?"Pending":'<span class="muted">Offline</span>'}${c.online&&!c.drops?' <span class="muted">(no drops)</span>':""}</td><td>${c.viewers??""}</td><td>${c.online&&!(w&&w.id===c.id)?`<button class="b" data-sw="${c.id}">Switch</button>`:""}</td></tr>`).join("");
  renderCampaigns();
  $("logs").textContent=S.log.join("\\n");
  const k=JSON.stringify([S.settings,S.games]);
  if(k!==setKey){setKey=k;renderSettings()}
}
function renderCampaigns(){
  const q=$("q").value.toLowerCase();
  $("campaigns").innerHTML=S.campaigns.filter(c=>!q||c.name.toLowerCase().includes(q)||c.game.toLowerCase().includes(q)).map(c=>
    `<div class="card"><div class="row"><b style="flex:1">${c.prioritized?"★ ":""}${esc(c.name)}</b><button class="b" data-pc="${esc(c.id)}">${c.prioritized?"★ Prioritized":"☆ Prioritize"}</button></div>
    <div class="muted">${esc(c.game)} · <span class="${c.status}">${c.status}</span> · ${when(c.starts_at)} – ${when(c.ends_at)}${c.linked?"":` · <a class="bad" href="${esc(c.link_url)}" target="_blank" rel="noopener">not linked</a>`}${c.can_finish||c.finished?"":' · <span class="bad">can\\'t finish in time</span>'}</div>
    <div>${c.claimed}/${c.total} claimed · ${pct(c.progress)}${c.finished?"":" · "+mins(c.remaining_minutes)+" left"}</div><div class="bar"><i style="width:${pct(c.progress)}"></i></div>
    <div class="drops">${c.drops.map(d=>`<div class="drop">${d.claimed?"✔ ":""}${esc(d.name)}<div class="bar"><i style="width:${pct(d.progress)}"></i></div><span class="muted">${mins(d.current)} / ${mins(d.required)}</span></div>`).join("")}</div></div>`).join("")||'<p class="muted">No campaigns</p>';
}
function listHtml(items,kind){
  return items.map((g,i)=>`<li><span>${esc(g)}</span>${kind==="p"?`<button class="b" data-up="${i}">↑</button><button class="b" data-down="${i}">↓</button>`:""}<button class="b" data-rm="${kind}${i}">✕</button></li>`).join("")||'<li class="muted">Empty</li>';
}
function renderSettings(){
  const s=S.settings;
  $("prio").innerHTML=listHtml(s.priority,"p");
  $("excl").innerHTML=listHtml(s.exclude,"e");
  $("mode").innerHTML=S.priority_modes.map(m=>`<option value="${m}"${m===s.priority_mode?" selected":""}>${m.replace(/_/g," ").toLowerCase()}</option>`).join("");
  $("avail").checked=s.available_drops_check;
  $("badges").checked=s.enable_badges_emotes;
  $("games").innerHTML=S.games.map(g=>`<option value="${esc(g)}">`).join("");
}
document.addEventListener("click",e=>{
  const t=e.target.closest("button");if(!t||!S)return;
  const s=S.settings,p=[...s.priority];
  if(t.dataset.sw)api("api/switch",{channel_id:+t.dataset.sw}).then(refresh);
  else if(t.dataset.pc){const id=t.dataset.pc,l=s.priority_campaigns.filter(x=>x!==id);if(l.length===s.priority_campaigns.length)l.push(id);save({priority_campaigns:l})}
  else if(t.dataset.up){const i=+t.dataset.up;if(i>0){[p[i-1],p[i]]=[p[i],p[i-1]];save({priority:p})}}
  else if(t.dataset.down){const i=+t.dataset.down;if(i<p.length-1){[p[i+1],p[i]]=[p[i],p[i+1]];save({priority:p})}}
  else if(t.dataset.rm){const k=t.dataset.rm[0],i=+t.dataset.rm.slice(1);if(k==="p"){p.splice(i,1);save({priority:p})}else save({exclude:s.exclude.filter((_,j)=>j!==i)})}
});
$("paddb").onclick=()=>{const g=$("padd").value.trim();if(g&&!S.settings.priority.includes(g)){$("padd").value="";save({priority:[...S.settings.priority,g]})}};
$("eaddb").onclick=()=>{const g=$("eadd").value.trim();if(g){$("eadd").value="";save({exclude:[...S.settings.exclude,g]})}};
$("mode").onchange=e=>save({priority_mode:e.target.value});
$("avail").onchange=e=>save({available_drops_check:e.target.checked});
$("badges").onchange=e=>save({enable_badges_emotes:e.target.checked});
$("reload").onclick=()=>api("api/reload",{}).then(refresh);
$("q").oninput=()=>S&&renderCampaigns();
async function refresh(){
  if(!token)return showLogin();
  try{S=await api("api/state");$("login").hidden=true;$("app").hidden=false;render()}catch(e){}
}
refresh();setInterval(refresh,5000);
</script>
</body>
</html>
"""
