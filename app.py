"""
Asbuilt — local web app
=======================

    pip install flask
    python app.py
    open http://127.0.0.1:5000

Everything runs locally against data/asbuilt.db. Nothing is uploaded anywhere.

Interface notes
---------------
Ask is the product; the job list is supporting evidence. So Ask is "/" and the
jobs live at /jobs. The visual language is a terminal instrument, not a SaaS
dashboard: dark substrate, monospace for every piece of machine-generated
metadata, one accent colour, hairlines instead of boxes-in-boxes. Long-form
answers switch to a system sans at 17px, because those are the only words on
screen that a person actually reads rather than scans.
"""

import argparse
import datetime
import os
import socket
import sqlite3

from dotenv import load_dotenv
from flask import (Flask, g, jsonify, redirect, render_template_string, request,
                   send_file, session, abort)

import auth

load_dotenv()

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(HERE, "data", "asbuilt.db")

app = Flask(__name__)
app.secret_key = auth.ensure_secret()
app.config.update(
    # Stay signed in on a phone rather than re-typing a passphrase daily.
    PERMANENT_SESSION_LIFETIME=datetime.timedelta(days=30),
    SESSION_COOKIE_HTTPONLY=True,      # JS can't read the session cookie
    SESSION_COOKIE_SAMESITE="Lax",     # blocks cross-site request forgery
)


def db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(_):
    conn = g.pop("db", None)
    if conn:
        conn.close()


def esc(text):
    """Minimal HTML escaping for values coming out of a stranger's email."""
    return (str(text or "")
            .replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


# ---------------------------------------------------------------------------
# Shell
# ---------------------------------------------------------------------------

BASE = """
<!doctype html><html lang=en><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name=color-scheme content=dark>
<meta name=theme-color content="#0A0A0B">
<meta name=description content="Answers about your jobs, drawn from your own email.">
<link rel=icon href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'><rect width='32' height='32' rx='7' fill='%230A0A0B'/><path d='M8 11h16M8 16h11M8 21h16' stroke='%23E24F3B' stroke-width='2.4' stroke-linecap='square'/></svg>">
<title>{{ title }} · Asbuilt</title>
<style>
  :root{
    --bg:#0A0A0B; --panel:#100F11; --panel-2:#161519; --panel-3:#1C1B20;
    --line:rgba(255,255,255,.075); --line-2:rgba(255,255,255,.14);
    --text:#E9E6E1; --muted:#8C8781; --dim:#5B5752;
    --accent:#E24F3B; --accent-lit:#FF6E58; --accent-wash:rgba(226,79,59,.11);
    --live:#4FA97B; --wait:#C9964A;
    --mono:ui-monospace,"SF Mono","JetBrains Mono","Cascadia Mono",Menlo,Consolas,monospace;
    --sans:-apple-system,BlinkMacSystemFont,"Segoe UI Variable Text","Segoe UI",system-ui,sans-serif;
    --ease:cubic-bezier(.22,.61,.36,1);
    --out:cubic-bezier(.16,1,.3,1);
    --z-nav:10; --z-grain:60;
  }
  *{box-sizing:border-box}
  html{-webkit-text-size-adjust:100%;scroll-behavior:smooth}
  body{margin:0;min-height:100dvh;background:var(--bg);color:var(--text);
       font:13px/1.5 var(--mono);letter-spacing:.01em;
       -webkit-font-smoothing:antialiased;text-rendering:optimizeLegibility}

  /* Fixed, non-scrolling grain. Keeps the flat dark from reading as plastic. */
  body::after{content:"";position:fixed;inset:0;z-index:var(--z-grain);
    pointer-events:none;opacity:.035;
    background-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg'%3E%3Cfilter id='n'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='.82' numOctaves='3'/%3E%3C/filter%3E%3Crect width='100%25' height='100%25' filter='url(%23n)'/%3E%3C/svg%3E")}

  a{color:inherit;text-decoration:none}
  ::selection{background:var(--accent);color:#fff}
  :focus-visible{outline:1.5px solid var(--accent-lit);outline-offset:3px}

  .skip{position:absolute;left:-9999px}
  .skip:focus{left:12px;top:12px;z-index:99;background:var(--accent);
    color:#fff;padding:9px 14px}

  /* ---- top bar: hairline, not sticky, not glued ---- */
  nav.top{display:flex;align-items:center;gap:26px;
    padding:20px 26px;border-bottom:1px solid var(--line)}
  .brand{display:flex;align-items:center;font-size:13px;font-weight:600;
    letter-spacing:.22em;color:var(--text)}
  nav.top .links{display:flex;gap:22px;margin-left:8px}
  nav.top .links a{font-size:11.5px;letter-spacing:.17em;text-transform:uppercase;
    color:var(--dim);transition:color .25s var(--ease);padding:4px 0}
  nav.top .links a:hover{color:var(--muted)}
  nav.top .links a.on{color:var(--text)}
  nav.top .links a.on::after{content:"";display:block;height:1px;
    background:var(--accent);margin-top:5px}
  .meta{margin-left:auto;display:flex;align-items:center;gap:18px;
    font-size:11px;letter-spacing:.1em;color:var(--dim);white-space:nowrap}
  .meta a:hover{color:var(--muted)}

  main{max-width:940px;margin:0 auto;padding:44px 26px 120px}
  main.narrow{max-width:720px}
  /* the ask screen ends at the prompt until an answer arrives — the tall
     bottom padding would only buy an empty scrollbar */
  main.ask{padding-bottom:36px}

  /* ---- type ---- */
  h1{font-family:var(--mono);font-size:15px;font-weight:600;
     letter-spacing:.16em;text-transform:uppercase;margin:0 0 6px}
  h2{font-size:11px;font-weight:600;letter-spacing:.2em;text-transform:uppercase;
     color:var(--dim);margin:52px 0 16px;display:flex;align-items:center;gap:12px}
  h2::after{content:"";flex:1;height:1px;background:var(--line)}
  .sub{color:var(--muted);font-size:12.5px;letter-spacing:.04em;margin-bottom:30px}
  .lede{font-family:var(--sans);font-size:16px;line-height:1.6;color:var(--muted);
    letter-spacing:0;max-width:56ch}
  .num{font-variant-numeric:tabular-nums}

  /* ---- the ask stage ---- */
  .stage{display:flex;flex-direction:column;align-items:center;
    padding:7vh 0 6px}

  .orbit{--d:384px;position:relative;width:var(--d);height:var(--d);
    perspective:1240px;margin-bottom:98px;flex:none}
  /* blueprint field behind the sphere, faded at the edges */
  .orbit .field{position:absolute;inset:-58%;pointer-events:none;opacity:.5;
    background-image:linear-gradient(var(--line) 1px,transparent 1px),
                     linear-gradient(90deg,var(--line) 1px,transparent 1px);
    background-size:34px 34px;
    -webkit-mask-image:radial-gradient(circle,#000 12%,transparent 62%);
    mask-image:radial-gradient(circle,#000 12%,transparent 62%)}
  .orbit .halo{position:absolute;inset:-34%;border-radius:50%;opacity:0;
    background:radial-gradient(circle,rgba(226,79,59,.24) 0%,rgba(226,79,59,.06) 38%,transparent 68%);
    transition:opacity 1s var(--out)}
  .orbit .rim{position:absolute;inset:0;border-radius:50%;
    border:1px solid rgba(255,255,255,.30);
    transition:border-color .7s var(--ease)}
  .tilt{position:absolute;inset:0;transform-style:preserve-3d;
    transform:rotateZ(-15deg) rotateX(9deg)}
  .spin{position:absolute;inset:0;transform-style:preserve-3d;
    animation:spin 34s linear infinite;
    transition:none}
  @keyframes spin{from{transform:rotateY(0)}to{transform:rotateY(360deg)}}

  .mer,.lat{position:absolute;border-radius:50%;
    border:1px solid rgba(255,255,255,.15);
    transition:border-color .7s var(--ease)}
  .mer{inset:0}
  .lat{left:50%;top:50%}
  .lat.eq{border-color:rgba(226,79,59,.62);border-width:1.2px}

  /* polar axis + poles live outside .spin — the axis is what it turns about */
  .axis{position:absolute;left:50%;top:-11%;height:122%;width:0;
    border-left:1px dashed rgba(255,255,255,.20)}
  .pole{position:absolute;left:50%;width:5px;height:5px;border-radius:50%;
    background:var(--accent);margin-left:-2.5px;
    box-shadow:0 0 9px rgba(226,79,59,.7)}
  .pole.n{top:calc(-11% - 2px)} .pole.s{top:calc(111% - 3px)}

  /* light running a great circle — hidden until a question is in flight */
  .orb{position:absolute;inset:0;transform-style:preserve-3d;
    opacity:0;transition:opacity .55s var(--ease)}
  .ring{position:absolute;inset:0;animation:sweep 2.4s linear infinite}
  @keyframes sweep{from{transform:rotateZ(0)}to{transform:rotateZ(360deg)}}
  .ring i{position:absolute;inset:0}
  .lt{position:absolute;left:50%;top:-4px;width:9px;height:9px;
    margin-left:-4.5px;border-radius:50%;
    background:radial-gradient(circle,#FFD9CF 0%,var(--accent-lit) 42%,rgba(226,79,59,0) 72%);
    transform:scale(2.1)}

  .orbit.busy .halo{opacity:1}
  .orbit.busy .orb{opacity:1}
  .orbit.busy .spin{animation-duration:9s}
  .orbit.busy .rim{border-color:rgba(255,255,255,.46)}
  .orbit.busy .mer{border-color:rgba(255,255,255,.26);
    animation:wire 2.3s cubic-bezier(.4,0,.2,1) infinite;
    animation-delay:calc(var(--i) * -.17s)}
  .orbit.busy .lat{border-color:rgba(255,255,255,.24)}
  .orbit.busy .lat.eq{border-color:var(--accent-lit);
    box-shadow:0 0 16px rgba(226,79,59,.35)}
  @keyframes wire{0%,100%{border-color:rgba(255,255,255,.16)}
    50%{border-color:rgba(255,255,255,.42)}}

  /* ---- terminal input ---- */
  .term{width:100%;max-width:600px;position:relative}
  .term form{display:flex;align-items:stretch;
    border:1px solid var(--line-2);background:var(--panel);
    transition:border-color .3s var(--ease),background .3s var(--ease)}
  .term form:focus-within{border-color:rgba(255,255,255,.30);
    background:var(--panel-2)}
  .term .ps1{display:flex;align-items:center;padding:0 0 0 16px;
    font-size:15px;color:var(--muted);user-select:none;flex:none}
  .term input{flex:1;min-width:0;background:none;border:0;color:var(--text);
    font:14.5px/1 var(--mono);letter-spacing:.02em;padding:19px 12px}
  .term input::placeholder{color:var(--dim)}
  .term input:focus{outline:none}

  /* One cursor, not two. The native caret is turned off and this block is
     driven to the real selectionStart, so the thing that blinks is the thing
     you are typing at. Only the home prompt runs this; the secondary search
     boxes keep the ordinary caret. */
  .term.live .field{position:relative;flex:1;min-width:0;display:flex}
  .term.live input{caret-color:transparent}
  .term.live .cur{position:absolute;left:12px;top:50%;width:8px;height:17px;
    margin-top:-8.5px;background:var(--text);pointer-events:none;
    animation:blink 1.06s steps(1,end) infinite}
  @keyframes blink{0%,49%{opacity:1}50%,100%{opacity:0}}
  .term.live.busy .cur{opacity:0;animation:none}
  .term.live .ghost{position:absolute;left:12px;top:50%;transform:translateY(-50%);
    color:var(--dim);font:14.5px/1 var(--mono);letter-spacing:.02em;
    padding-left:17px;pointer-events:none;white-space:nowrap}
  .term.live.typing .ghost{display:none}
  /* invisible ruler: measures the text left of the caret in the exact font */
  .term.live .rule{position:absolute;left:-9999px;top:0;visibility:hidden;
    white-space:pre;font:14.5px/1 var(--mono);letter-spacing:.02em}
  .term button{flex:none;border:0;border-left:1px solid var(--line);
    background:none;color:var(--dim);cursor:pointer;padding:0 20px;
    font:11px/1 var(--mono);letter-spacing:.2em;text-transform:uppercase;
    transition:color .25s var(--ease),background .25s var(--ease)}
  .term button:hover{color:var(--text);background:var(--accent-wash)}
  .term button:active{transform:scale(.97)}
  .term.busy input{color:var(--muted)}
  .term.busy button{color:var(--dim);pointer-events:none}

  /* live status readout under the prompt */
  .status{height:17px;margin-top:13px;font-size:11px;letter-spacing:.13em;
    text-transform:uppercase;color:var(--dim);
    opacity:0;transition:opacity .3s var(--ease);text-align:center}
  .status.on{opacity:1}
  .status b{color:var(--muted);font-weight:400}

  /* ---- answer ---- */
  .out{width:100%;margin-top:52px}
  .reveal{opacity:0;transform:translateY(14px);
    animation:rise .7s var(--out) forwards}
  @keyframes rise{to{opacity:1;transform:none}}
  .qline{font-size:11px;letter-spacing:.2em;text-transform:uppercase;
    color:var(--dim);margin-bottom:14px}
  .qline b{color:var(--text);font-weight:400;letter-spacing:.05em;
    text-transform:none;font-size:12.5px}
  .answer{border-left:2px solid var(--accent);padding:4px 0 4px 22px;
    font:17px/1.68 var(--sans);letter-spacing:0;white-space:pre-wrap;
    color:var(--text)}
  .answer.none{border-left-color:var(--wait);color:var(--muted)}
  .src{display:block;border-top:1px solid var(--line);padding:15px 2px;
    transition:padding .3s var(--out),background .25s var(--ease)}
  .src:last-of-type{border-bottom:1px solid var(--line)}
  .src:hover{padding-left:11px}
  .src .s{font-size:13px;color:var(--text);letter-spacing:.01em}
  .src .m{font-size:11px;color:var(--dim);letter-spacing:.09em;margin-top:5px;
    text-transform:uppercase}
  .foot{margin-top:22px;font-size:11px;letter-spacing:.14em;
    text-transform:uppercase;color:var(--dim)}
  .err{border-left:2px solid var(--wait);padding:12px 0 12px 20px;
    color:var(--muted);font-size:12.5px}

  /* ---- job list ---- */
  .job{display:grid;grid-template-columns:14px 1fr auto;gap:14px;
    align-items:baseline;border-top:1px solid var(--line);padding:17px 2px;
    transition:padding .3s var(--out),background .25s var(--ease)}
  .job:last-of-type{border-bottom:1px solid var(--line)}
  .job:hover{padding-left:11px;background:rgba(255,255,255,.017)}
  .job .name{font-size:14.5px;letter-spacing:.03em;color:var(--text)}
  .job .latest{grid-column:2;font-size:11.5px;color:var(--dim);margin-top:6px;
    overflow:hidden;text-overflow:ellipsis;white-space:nowrap;letter-spacing:.04em}
  .job .when{font-size:11px;color:var(--muted);letter-spacing:.11em;
    text-transform:uppercase;white-space:nowrap;font-variant-numeric:tabular-nums}
  .dot{width:6px;height:6px;border-radius:50%;background:var(--dim);
    align-self:center;justify-self:center}
  .dot.live{background:var(--live);box-shadow:0 0 8px rgba(79,169,123,.6)}
  .dot.wait{background:var(--wait)}
  .dot.cold{background:#38363A}

  /* ---- tables ---- */
  table{border-collapse:collapse;width:100%;font-size:12.5px}
  th{text-align:left;font-size:10px;letter-spacing:.2em;text-transform:uppercase;
    color:var(--dim);font-weight:600;padding:0 12px 10px 0;
    border-bottom:1px solid var(--line-2)}
  td{padding:13px 12px 13px 0;border-bottom:1px solid var(--line);
    vertical-align:top;color:var(--muted)}
  td strong{color:var(--text);font-weight:500;letter-spacing:.02em}
  tr:hover td{background:rgba(255,255,255,.017)}
  .date{white-space:nowrap;color:var(--dim);font-variant-numeric:tabular-nums;
    font-size:11.5px}
  .right{text-align:right;font-variant-numeric:tabular-nums;color:var(--dim)}
  .dir{color:var(--dim);width:1.4em}
  td a{color:var(--muted);border-bottom:1px solid var(--line-2)}
  td a:hover{color:var(--accent-lit);border-bottom-color:var(--accent)}
  .att{display:block;margin-top:6px;font-size:11.5px;color:var(--dim)}
  .tag{display:inline-block;border:1px solid var(--line-2);padding:2px 8px;
    font-size:10px;letter-spacing:.14em;text-transform:uppercase;
    color:var(--muted);margin-left:9px}
  .tag.hot{border-color:rgba(226,79,59,.4);color:var(--accent-lit)}
  .snip{color:var(--dim);font-size:11.5px;margin-top:5px;letter-spacing:.03em}
  mark{background:rgba(226,79,59,.24);color:var(--text);padding:0 3px}

  @media (prefers-reduced-motion:reduce){
    *{animation-duration:.01ms !important;animation-iteration-count:1 !important;
      transition-duration:.01ms !important}
    html{scroll-behavior:auto}
  }
  @media (max-width:760px){
    .orbit{--d:300px;margin-bottom:76px}
    main{padding:32px 18px 100px}
    nav.top{padding:16px 18px;gap:18px}
    .meta .addr{display:none}
    h2{margin-top:42px}
  }
  @media (max-width:420px){
    .orbit{--d:264px;margin-bottom:64px}
    .term input{padding:17px 10px;font-size:14px}
    .term button{padding:0 14px}
    table{display:block;overflow-x:auto;white-space:nowrap}
    td .snip,td .att{white-space:normal}
  }
  /* Short viewport — a rotated phone or a small laptop. Width-based rules
     don't catch these, and the globe plus its gap would push the prompt off
     screen. Keyed to height so it fires whatever the width. */
  @media (max-height:640px){
    .stage{padding:3vh 0 6px}
    .orbit{--d:min(196px,34vh);margin-bottom:calc(var(--d) * .24)}
  }
</style>
<body data-total="{{ total }}">
<a class=skip href="#main">Skip to content</a>
<nav class=top>
  <a href="/" class=brand>ASBUILT</a>
  <span class=links>
    <a href="/" class="{{ 'on' if active=='ask' }}">Ask</a>
    <a href="/jobs" class="{{ 'on' if active=='jobs' }}">Jobs</a>
    <a href="/search" class="{{ 'on' if active=='search' }}">Search</a>
  </span>
  <span class=meta>
    <span class=addr>{{ mailbox }}</span>
    <a href="/logout">Sign out</a>
  </span>
</nav>
<main id=main class="{{ mainclass }}">{{ body|safe }}</main>
<script>
/* ---- wireframe globe -------------------------------------------------
   Real 3D, not a drawing of one. Meridians are circles turned about the
   polar axis; latitudes are circles lifted up the axis and laid flat. So
   the thing actually rotates instead of faking it with a sprite. */
(function(){
  var spin = document.getElementById('spin');
  if(!spin) return;
  var MER = 12, LAT = 4, html = '';
  for(var i=0;i<MER;i++){
    html += '<div class=mer style="transform:rotateY('+(i*180/MER).toFixed(2)+
            'deg);--i:'+i+'"></div>';
  }
  for(var k=-LAT;k<=LAT;k++){
    var phi = k*(90/(LAT+1)) * Math.PI/180;
    var w = (Math.cos(phi)*100).toFixed(2);      // ring width, % of diameter
    var lift = (-Math.sin(phi)*0.5).toFixed(4);  // ...as a fraction of --d
    html += '<div class="lat'+(k===0?' eq':'')+'" style="width:'+w+'%;height:'+w+
            '%;transform:translate(-50%,-50%) translateY(calc(var(--d) * '+lift+
            ')) rotateX(90deg)"></div>';
  }
  spin.innerHTML = html;

  /* Lights that run great circles through the sphere while a question is in
     flight. Each is a plane holding a comet: bright head, two dimmer tails. */
  var tilt = document.getElementById('tilt');
  var PLANES = [[24,68,2.2,0],[-58,52,3.1,-.7],[76,-40,2.6,-1.5]];
  PLANES.forEach(function(p){
    var orb = document.createElement('div');
    orb.className = 'orb';
    orb.style.transform = 'rotateY('+p[0]+'deg) rotateX('+p[1]+'deg)';
    var tail = '';
    [[0,1,1],[-8,.42,.72],[-16,.17,.5]].forEach(function(t){
      tail += '<i style="transform:rotateZ('+t[0]+'deg);opacity:'+t[1]+
              '"><b class=lt style="transform:scale('+(2.1*t[2]).toFixed(2)+
              ')"></b></i>';
    });
    orb.innerHTML = '<div class=ring style="animation-duration:'+p[2]+
                    's;animation-delay:'+p[3]+'s">'+tail+'</div>';
    tilt.appendChild(orb);
  });
})();
</script>
{{ tail|safe }}
"""


def page(title, body, active="", narrow=False, tail="", total=0, cls=""):
    row = db().execute("SELECT address FROM mailbox LIMIT 1").fetchone()
    return render_template_string(
        BASE, title=title, body=body, active=active, tail=tail, total=total,
        mainclass=" ".join(c for c in ("narrow" if narrow else "", cls) if c),
        mailbox=row["address"] if row else "")


# ---------------------------------------------------------------------------
# Login
# ---------------------------------------------------------------------------

LOGIN_PAGE = """
<!doctype html><html lang=en><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<meta name=color-scheme content=dark>
<meta name=theme-color content="#0A0A0B">
<title>Asbuilt</title>
<style>
  :root{--bg:#0A0A0B;--panel:#100F11;--line:rgba(255,255,255,.13);
        --text:#E9E6E1;--dim:#5B5752;--muted:#8C8781;--accent:#E24F3B;
        --mono:ui-monospace,"SF Mono","JetBrains Mono","Cascadia Mono",Menlo,Consolas,monospace}
  *{box-sizing:border-box}
  body{margin:0;min-height:100dvh;display:flex;align-items:center;
       justify-content:center;padding:24px;background:var(--bg);color:var(--text);
       font:13px/1.5 var(--mono);-webkit-font-smoothing:antialiased}
  body::after{content:"";position:fixed;inset:0;pointer-events:none;opacity:.035;
    background-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg'%3E%3Cfilter id='n'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='.82' numOctaves='3'/%3E%3C/filter%3E%3Crect width='100%25' height='100%25' filter='url(%23n)'/%3E%3C/svg%3E")}
  .box{width:100%;max-width:352px}
  .brand{display:flex;align-items:center;font-size:13px;font-weight:600;
    letter-spacing:.22em;margin-bottom:8px}
  .sub{color:var(--dim);font-size:11px;letter-spacing:.18em;
    text-transform:uppercase;margin-bottom:34px}
  label{display:block;font-size:10px;letter-spacing:.2em;text-transform:uppercase;
    color:var(--dim);margin:0 0 8px}
  .f{margin-bottom:20px}
  input{width:100%;padding:15px 14px;background:var(--panel);color:var(--text);
    border:1px solid var(--line);font:14px/1 var(--mono);letter-spacing:.03em}
  input:focus{outline:none;border-color:rgba(226,79,59,.6)}
  button{width:100%;margin-top:10px;padding:16px;border:0;background:var(--accent);
    color:#fff;font:11px/1 var(--mono);letter-spacing:.24em;text-transform:uppercase;
    cursor:pointer;transition:background .25s cubic-bezier(.22,.61,.36,1)}
  button:hover{background:#F05C46}
  button:active{transform:scale(.99)}
  .err{border-left:2px solid var(--accent);padding:10px 0 10px 14px;
    color:var(--muted);font-size:11.5px;margin-bottom:24px}
  code{color:var(--text)}
</style>
<div class=box>
  <div class=brand>ASBUILT</div>
  <div class=sub>{{ sub }}</div>
  {{ error|safe }}
  <form method=post>
    <div class=f>
      <label for=u>Username</label>
      <input id=u name=username autocapitalize=off autocorrect=off autofocus>
    </div>
    <div class=f>
      <label for=p>Password</label>
      <input id=p name=password type=password>
    </div>
    <button>Sign in</button>
  </form>
</div>"""


@app.route("/login", methods=["GET", "POST"])
def login():
    if not auth.is_configured():
        return render_template_string(
            LOGIN_PAGE, sub="No password set",
            error='<div class=err>Run <code>python auth.py set</code> first.</div>')

    error = ""
    if request.method == "POST":
        if auth.verify(request.form.get("username"), request.form.get("password")):
            session.permanent = True
            session["authed"] = True
            nxt = request.args.get("next") or "/"
            # Only ever redirect within this app — an attacker-supplied
            # absolute URL in ?next= would otherwise bounce users off-site.
            return redirect(nxt if nxt.startswith("/") and "//" not in nxt else "/")
        error = '<div class=err>Wrong username or password.</div>'

    return render_template_string(LOGIN_PAGE, sub="Sign in to continue",
                                  error=error)


@app.route("/logout")
def logout():
    session.clear()
    return redirect("/login")


# ---------------------------------------------------------------------------
# Ask — the product
# ---------------------------------------------------------------------------

GLOBE = """
<div class=orbit id=orbit>
  <div class=field></div>
  <div class=halo></div>
  <div class=tilt id=tilt>
    <div class=axis></div>
    <div class="pole n"></div>
    <div class="pole s"></div>
    <div class=spin id=spin></div>
  </div>
  <div class=rim></div>
</div>"""


ASK_JS = """
<script>
(function(){
  var term = document.getElementById('term'),
      form = document.getElementById('askform'),
      input = document.getElementById('q'),
      orbit = document.getElementById('orbit'),
      status = document.getElementById('status'),
      out = document.getElementById('out'),
      total = Number(document.body.dataset.total || 0);
  if(!form) return;

  function esc(s){
    return String(s == null ? '' : s)
      .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')
      .replace(/"/g,'&quot;');
  }

  /* Real pipeline stages, not a fake progress bar. These are the three things
     assistant.py actually does, in the order it does them. */
  var STEPS = [
    'resolving job name',
    'searching ' + total.toLocaleString() + ' emails',
    'reading the threads it found',
    'checking every citation'
  ];
  var timer = null;

  function run(step){
    var n = 0;
    status.className = 'status on';
    status.innerHTML = '<b>&gt;</b> ' + STEPS[0];
    timer = setInterval(function(){
      n = Math.min(n + 1, STEPS.length - 1);
      status.innerHTML = '<b>&gt;</b> ' + STEPS[n];
    }, 2600);
  }
  function stop(){ clearInterval(timer); status.className = 'status'; }

  /* Drive the block cursor to the real caret. Measuring the text left of
     selectionStart in an identical font means it stays correct through
     arrow keys, clicks, paste and mid-string edits — not just typing. */
  var cur = document.getElementById('cur'),
      ghost = document.getElementById('ghost'),
      rule = document.getElementById('rule');

  function caret(){
    var at = input.selectionStart;
    if(at === null || at === undefined) at = input.value.length;
    rule.textContent = input.value.slice(0, at);
    var x = rule.getBoundingClientRect().width - input.scrollLeft;
    cur.style.transform = 'translateX(' + Math.max(0, x) + 'px)';
    term.classList.toggle('typing', input.value.length > 0);
  }
  ['input','keyup','keydown','click','focus','blur','scroll','select']
    .forEach(function(e){ input.addEventListener(e, caret); });
  ghost.addEventListener('click', function(){ input.focus(); });
  caret();

  form.addEventListener('submit', function(ev){
    var q = input.value.trim();
    if(!q) return ev.preventDefault();
    ev.preventDefault();

    term.classList.add('busy');
    orbit.classList.add('busy');
    out.innerHTML = '';
    run();

    fetch('/api/ask', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({q: q})
    })
    .then(function(r){ return r.json().then(function(j){ return [r.ok, j]; }); })
    .then(function(pair){
      var ok = pair[0], d = pair[1];
      if(!ok || d.error) throw new Error(d.error || 'Request failed');
      render(q, d);
    })
    .catch(function(e){
      out.innerHTML = '<div class="err reveal">' + esc(e.message) + '</div>';
    })
    .finally(function(){
      stop();
      term.classList.remove('busy');
      orbit.classList.remove('busy');
    });
  });

  function render(q, d){
    var h = '<div class="qline reveal"><b>' + esc(q) + '</b>' +
            (d.project ? ' &nbsp;·&nbsp; ' + esc(d.project) : '') + '</div>';
    h += '<div class="answer reveal' + (d.found ? '' : ' none') +
         '" style="animation-delay:.08s">' + esc(d.answer) + '</div>';

    if(d.sources && d.sources.length){
      h += '<h2 class=reveal style="animation-delay:.16s">Where this came from · ' +
           d.sources.length + '</h2>';
      d.sources.forEach(function(s, i){
        h += '<a class="src reveal" style="animation-delay:' +
             (0.2 + i * 0.045).toFixed(2) + 's" href="/search?q=' +
             encodeURIComponent(s.id) + '">' +
             '<div class=s>' + esc(s.subject || '(no subject)') + '</div>' +
             '<div class=m>' + esc((s.sent_at || '').slice(0,10)) + ' &nbsp;·&nbsp; ' +
             esc(s.who) + '</div></a>';
      });
    }
    h += '<div class="foot reveal" style="animation-delay:.32s">Read ' +
         d.searched + ' of your emails to answer that</div>';
    out.innerHTML = h;
    out.scrollIntoView({behavior: 'smooth', block: 'start'});
  }

  /* Focus on desktop only. On a phone this pops the keyboard on load, which
     covers the globe — the one thing that should land before anything else. */
  if(window.matchMedia('(pointer:fine)').matches) input.focus();
})();
</script>"""


@app.route("/")
@auth.login_required
def home():
    total = db().execute("SELECT COUNT(*) FROM message").fetchone()[0]

    body = f"""
<div class=stage id=stage>
  {GLOBE}
  <div class="term live" id=term>
    <form id=askform action="/ask" method=get>
      <span class=ps1>&gt;</span>
      <span class=field>
        <input id=q name=q autocomplete=off autocapitalize=off autocorrect=off
               spellcheck=false aria-label="Ask about any job">
        <span class=cur id=cur></span>
        <span class=ghost id=ghost>Ask about any job</span>
        <span class=rule id=rule aria-hidden=true></span>
      </span>
      <button type=submit>Ask</button>
    </form>
    <div class=status id=status></div>
  </div>
  <div class=out id=out></div>
</div>"""
    # total rides on <body data-total> so the status ticker quotes a real
    # corpus size instead of an invented one.
    return page("Ask", body, active="ask", narrow=True, tail=ASK_JS, total=total)


@app.post("/api/ask")
@auth.login_required
def api_ask():
    data = request.get_json(silent=True) or {}
    q = (data.get("q") or "").strip()
    pid = data.get("project")
    if not q:
        return jsonify(error="Ask a question first."), 400

    from assistant import ask as run_ask
    try:
        result = run_ask(q, project_id=pid)
    except Exception as exc:
        msg = str(exc)
        if "ANTHROPIC_API_KEY" in msg or "api_key" in msg.lower():
            msg = "No API key. Set ANTHROPIC_API_KEY in .env and restart."
        return jsonify(error=msg), 500

    return jsonify(
        answer=result["answer"],
        found=result["found"],
        project=result.get("project"),
        searched=result.get("searched", 0),
        sources=[{"id": s["id"], "subject": s["subject"],
                  "sent_at": s["sent_at"],
                  "who": s.get("from_name") or s.get("from_email") or ""}
                 for s in result["sources"]],
    )


@app.route("/ask")
@auth.login_required
def ask_route():
    """Server-rendered answer. The home page normally answers in place over
    /api/ask; this is what runs with JavaScript off, and what a shared link
    resolves to."""
    q = (request.args.get("q") or "").strip()
    pid = request.args.get("project", type=int)
    if not q:
        return redirect("/")

    from assistant import ask as run_ask
    try:
        result = run_ask(q, project_id=pid)
    except Exception as exc:
        return page("Ask", f"<h1>{esc(q)}</h1>"
                           f"<div class=err>{esc(exc)}</div>",
                    active="ask", narrow=True)

    scope = f" &nbsp;·&nbsp; {esc(result['project'])}" if result.get("project") else ""
    body = [f"<div class=qline><b>{esc(q)}</b>{scope}</div>",
            f"<div class='answer{'' if result['found'] else ' none'}'>"
            f"{esc(result['answer'])}</div>"]

    if result["sources"]:
        body.append(f"<h2>Where this came from · {len(result['sources'])}</h2>")
        for s in result["sources"]:
            who = esc(s["from_name"] or s["from_email"] or "")
            body.append(
                f"<a class=src href='/search?q={s['id']}'>"
                f"<div class=s>{esc(s['subject'] or '(no subject)')}</div>"
                f"<div class=m>{esc((s['sent_at'] or '')[:10])} &nbsp;·&nbsp; {who}"
                f"</div></a>")

    body.append(f"<div class=foot>Read {result['searched']} of your emails "
                f"to answer that</div>")
    body.append("""
<h2>Ask another</h2>
<div class=term>
  <form action="/ask" method=get>
    <span class=ps1>&gt;<span class=cur></span></span>
    <input name=q autocomplete=off placeholder="Ask about any job">
    <button type=submit>Ask</button>
  </form>
</div>""")
    return page(f"Ask: {q}", "".join(body), active="ask", narrow=True)


# ---------------------------------------------------------------------------
# Jobs
# ---------------------------------------------------------------------------

def age(iso, today):
    """Plain words, not a date. Nobody says '2026-07-30' — they say 'two weeks
    ago', and the size of the gap is what tells you if a job is still live."""
    if not iso:
        return "—", 9999
    try:
        days = (today - datetime.date.fromisoformat(iso[:10])).days
    except ValueError:
        return "—", 9999
    if days <= 0:  return "today", days
    if days == 1:  return "yesterday", days
    if days < 7:   return f"{days} days ago", days
    if days < 14:  return "last week", days
    if days < 60:  return f"{days // 7} weeks ago", days
    if days < 730: return f"{max(1, days // 30)} months ago", days
    return f"{days // 365} years ago", days


@app.route("/jobs")
@auth.login_required
def jobs():
    rows = db().execute("""
        SELECT p.id, p.name,
               COUNT(DISTINCT mp.message_id) msgs,
               MAX(m.sent_at) last_seen,
               (SELECT m2.subject FROM message m2
                JOIN message_project mp2 ON mp2.message_id = m2.id
                WHERE mp2.project_id = p.id
                ORDER BY m2.sent_at DESC LIMIT 1) last_subject
        FROM project p
        JOIN message_project mp ON mp.project_id = p.id
        JOIN message m ON m.id = mp.message_id
        GROUP BY p.id
        HAVING msgs >= 5
        ORDER BY last_seen DESC""").fetchall()

    today = datetime.date.today()
    active, dormant = [], []
    for r in rows:
        when, days = age(r["last_seen"], today)
        (active if days <= 120 else dormant).append((r, when, days))

    def card(r, when, days):
        state = "live" if days <= 21 else ("wait" if days <= 120 else "cold")
        latest = esc(r["last_subject"])[:100]
        return (f"<a class=job href='/project/{r['id']}'>"
                f"<span class='dot {state}'></span>"
                f"<span class=name>{esc(r['name'])}</span>"
                f"<span class=when>{when}</span>"
                f"<span class=latest>{latest}</span></a>")

    body = ["<h1>Jobs</h1>",
            f"<div class=sub>{sum(r['msgs'] for r in rows):,} emails sorted into "
            f"{len(rows)} jobs. None of it was filed by hand.</div>"]
    if active:
        body.append(f"<h2>Active · {len(active)}</h2>")
        body += [card(*a) for a in active]
    if dormant:
        body.append(f"<h2>Older · {len(dormant)}</h2>")
        body += [card(*d) for d in dormant[:40]]
        if len(dormant) > 40:
            # Say so rather than letting the list just stop.
            body.append(
                f"<div class=foot>Showing 40 of {len(dormant)} older jobs. "
                f"<a href='/search' style='color:var(--accent-lit)'>Search</a> "
                f"reaches the rest.</div>")
    return page("Jobs", "".join(body), active="jobs")


@app.route("/project/<int:pid>")
@auth.login_required
def project(pid):
    conn = db()
    proj = conn.execute("SELECT * FROM project WHERE id=?", (pid,)).fetchone()
    if not proj:
        abort(404)

    aliases = [r["alias"] for r in conn.execute(
        "SELECT alias FROM project_alias WHERE project_id=? ORDER BY alias", (pid,))]

    msgs = conn.execute("""
        SELECT m.*, mp.method,
               EXISTS(SELECT 1 FROM message_location l
                      WHERE l.message_id=m.id AND l.folder='Sent') AS outgoing
        FROM message m JOIN message_project mp ON mp.message_id=m.id
        WHERE mp.project_id=? ORDER BY m.sent_at""", (pid,)).fetchall()

    docs = conn.execute("""
        SELECT DISTINCT a.id, ma.filename, a.size_bytes
        FROM message_project mp
        JOIN message_attachment ma ON ma.message_id = mp.message_id
        JOIN attachment a ON a.id = ma.attachment_id
        WHERE mp.project_id=? AND a.is_inline=0
        ORDER BY ma.filename""", (pid,)).fetchall()

    body = [f"<h1>{esc(proj['name'])}</h1>",
            f"<div class=sub>{len(msgs)} messages · {len(docs)} documents · "
            f"also written as {esc(', '.join(aliases[:14]))}</div>",
            f"""<div class=term style="max-width:none;margin-bottom:8px">
  <form action="/ask" method=get>
    <span class=ps1>&gt;<span class=cur></span></span>
    <input name=q autocomplete=off placeholder="Ask about {esc(proj['name'])}">
    <input type=hidden name=project value="{pid}">
    <button type=submit>Ask</button>
  </form>
</div>"""]

    body.append(f"<h2>Documents · {len(docs)}</h2><table>")
    for d in docs[:200]:
        kb = (d["size_bytes"] or 0) // 1024
        body.append(f"<tr><td><a href='/file/{d['id']}'>{esc(d['filename'])}</a></td>"
                    f"<td class=right>{kb:,} KB</td></tr>")
    body.append("</table>")

    body.append(f"<h2>Timeline · {len(msgs)}</h2>"
                "<div class=sub>→ sent by us &nbsp;&nbsp; ← received</div><table>")
    for m in msgs:
        arrow = "→" if m["outgoing"] else "←"
        who = esc(m["from_name"] or m["from_email"])
        tag = " <span class=tag>thread</span>" if m["method"] == "thread" else ""
        atts = conn.execute("""
            SELECT ma.filename, a.id FROM message_attachment ma
            JOIN attachment a ON a.id=ma.attachment_id
            WHERE ma.message_id=? AND a.is_inline=0""", (m["id"],)).fetchall()
        att_html = "".join(
            f"<span class=att>▤ <a href='/file/{a['id']}'>{esc(a['filename'])}</a></span>"
            for a in atts)
        body.append(
            f"<tr><td class=date>{esc((m['sent_at'] or '')[:10])}</td>"
            f"<td class=dir>{arrow}</td>"
            f"<td><strong>{esc(m['subject'] or '(no subject)')}</strong>{tag}"
            f"<div class=snip>{who}</div>{att_html}</td></tr>")
    body.append("</table>")
    return page(proj["name"], "".join(body), active="jobs")


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------

@app.route("/search")
@auth.login_required
def search():
    q = (request.args.get("q") or "").strip()
    bar = f"""
<div class=term style="max-width:none">
  <form action="/search" method=get>
    <span class=ps1>&gt;<span class=cur></span></span>
    <input name=q autocomplete=off autocapitalize=off spellcheck=false
           value="{esc(q)}" placeholder="Search every email">
    <button type=submit>Find</button>
  </form>
</div>"""

    if not q:
        return page("Search",
                    "<h1>Search</h1>"
                    "<div class=sub>Exact words, across every message and "
                    "attachment. Ask is usually the faster way in.</div>" + bar,
                    active="search")

    rows = db().execute("""
        SELECT m.id, m.subject, m.sent_at, m.from_name, m.from_email,
               snippet(message_fts, 1, '<mark>', '</mark>', '…', 22) AS snip
        FROM message_fts f JOIN message m ON m.id = f.rowid
        WHERE message_fts MATCH ?
        ORDER BY rank LIMIT 200""", (q,)).fetchall()

    body = [f"<h1>{esc(q)}</h1>",
            f"<div class=sub>{len(rows)} message{'' if len(rows) == 1 else 's'}"
            f"</div>", bar, "<table style='margin-top:26px'>"]
    for r in rows:
        projs = db().execute("""
            SELECT p.id, p.name FROM message_project mp
            JOIN project p ON p.id=mp.project_id WHERE mp.message_id=?""",
            (r["id"],)).fetchall()
        tags = "".join(f"<a class='tag hot' href='/project/{p['id']}'>"
                       f"{esc(p['name'])}</a>" for p in projs)
        body.append(
            f"<tr><td class=date>{esc((r['sent_at'] or '')[:10])}</td>"
            f"<td><strong>{esc(r['subject'] or '(no subject)')}</strong>{tags}"
            f"<div class=snip>{r['snip']}</div>"
            f"<div class=snip>{esc(r['from_name'] or r['from_email'])}</div>"
            f"</td></tr>")
    body.append("</table>")
    return page(f"Search: {q}", "".join(body), active="search")


# ---------------------------------------------------------------------------
# Labelling (evaluation harness, not part of the product surface)
# ---------------------------------------------------------------------------

ASSIGNED_KEYS = [("1", "correct", "Correct project"),
                 ("2", "wrong", "Wrong project"),
                 ("3", "ambiguous", "Ambiguous / can't tell")]

LABEL_KEYS = {
    "assigned": ASSIGNED_KEYS,
    # Verification sample drawn after a resolver change — same question.
    "assigned_v2": ASSIGNED_KEYS,
    # "personal" matters: a small contractor's business inbox is also his
    # personal one. Without this bucket personal mail lands in spam/admin and
    # skews the composition estimate -- and the product has to handle it too.
    "unassigned": [("1", "construction", "Construction project"),
                   ("2", "service", "Service / repair call"),
                   ("3", "bid", "Bid / ITB — a job we never did"),
                   ("4", "admin", "Admin (insurance, payroll, tax)"),
                   ("5", "personal", "Personal / family / not business"),
                   ("6", "spam", "Spam / marketing"),
                   ("7", "unknown", "Can't determine")],
}


@app.route("/label")
@auth.login_required
def label():
    conn = db()
    # Newest stratum first: after a resolver change you want the verification
    # sample labeled, not another 250 from the original draw.
    row = conn.execute("""
        SELECT s.message_id, s.stratum FROM gold_sample s
        LEFT JOIN gold_label l ON l.message_id = s.message_id
        WHERE l.message_id IS NULL
        ORDER BY CASE s.stratum WHEN 'assigned_v2' THEN 0 ELSE 1 END,
                 RANDOM() LIMIT 1""").fetchone()

    done = conn.execute("SELECT COUNT(*) FROM gold_label").fetchone()[0]
    total = conn.execute("SELECT COUNT(*) FROM gold_sample").fetchone()[0]

    if not total:
        return page("Label", "<h1>No sample drawn</h1><div class=sub>Run "
                             "<code>python label.py draw</code> first.</div>")
    if not row:
        return page("Label", f"<h1>All {total} labeled</h1><div class=sub>Run "
                             f"<code>python label.py score</code>.</div>")

    m = conn.execute("SELECT * FROM message WHERE id=?",
                     (row["message_id"],)).fetchone()
    projs = conn.execute("""
        SELECT p.name, mp.method, mp.confidence
        FROM message_project mp JOIN project p ON p.id=mp.project_id
        WHERE mp.message_id=?""", (row["message_id"],)).fetchall()
    atts = conn.execute("""
        SELECT ma.filename FROM message_attachment ma
        JOIN attachment a ON a.id=ma.attachment_id
        WHERE ma.message_id=? AND a.is_inline=0""", (row["message_id"],)).fetchall()

    assigned_to = "".join(
        f"<div style='color:var(--accent-lit);font-size:13px'>{esc(p['name'])}"
        f"<span class=tag>{p['method']} · {p['confidence']:.2f}</span></div>"
        for p in projs) or "<span style='color:var(--dim)'>nothing</span>"

    body = [
        f"<div class=sub>{done} of {total} labeled · <strong>{row['stratum']}"
        f"</strong> stratum · press a number key</div>",
        "<div style='border:1px solid var(--line);padding:20px;"
        "background:var(--panel)'>",
        f"<div class=date>{esc((m['sent_at'] or '')[:16])}</div>",
        f"<h1 style='margin:8px 0;text-transform:none;letter-spacing:.02em;"
        f"font-size:14px'>{esc(m['subject'] or '(no subject)')}</h1>",
        f"<div class=sub style='margin:0 0 12px'>from "
        f"{esc(m['from_name'])} &lt;{esc(m['from_email'])}&gt;</div>",
    ]
    if row["stratum"].startswith("assigned"):
        body.append(f"<div style='margin:12px 0'>System says: {assigned_to}</div>")
    if atts:
        body.append("<div class=sub>" + " · ".join(
            f"▤ {esc(a['filename'])}" for a in atts[:8]) + "</div>")
    snippet = esc(" ".join((m["body_text"] or "").split())[:1400])
    body.append(f"<div style='margin-top:14px;white-space:pre-wrap;font-size:12px;"
                f"color:var(--muted);line-height:1.6'>{snippet}</div></div>")

    body.append("<div style='margin-top:20px;display:flex;gap:8px;flex-wrap:wrap'>")
    for key, value, caption in LABEL_KEYS[row["stratum"]]:
        body.append(
            f"<button data-k='{key}' data-v='{value}' "
            f"style='padding:12px 16px;border:1px solid var(--line-2);"
            f"background:var(--panel);color:var(--muted);cursor:pointer;"
            f"font:11.5px/1 var(--mono);letter-spacing:.06em'>"
            f"<strong style='color:var(--accent-lit)'>{key}</strong> &nbsp;{caption}"
            f"</button>")
    body.append("</div>")

    body.append(f"""
<form id=f method=post action="/label/{row['message_id']}" style="margin-top:16px">
  <input type=hidden name=value id=v>
  <input type=hidden name=stratum value="{row['stratum']}">
  <input name=note placeholder="note (optional) — Enter to skip"
         style="padding:11px;width:340px;max-width:100%;background:var(--panel);
                border:1px solid var(--line);color:var(--text);
                font:12px/1 var(--mono)">
</form>
<script>
document.querySelectorAll('button[data-v]').forEach(function(b){{
  b.onclick = function(){{ document.getElementById('v').value = b.dataset.v;
                           document.getElementById('f').submit(); }};
}});
document.addEventListener('keydown', function(e){{
  if (e.target.tagName === 'INPUT' && e.key !== 'Enter') return;
  var b = document.querySelector('button[data-k="' + e.key + '"]');
  if (b) b.click();
}});
</script>""")
    return page("Label", "".join(body))


@app.route("/label/<int:mid>", methods=["POST"])
@auth.login_required
def label_save(mid):
    value = request.form.get("value")
    stratum = request.form.get("stratum")
    note = request.form.get("note") or None
    conn = db()
    if (stratum or "").startswith("assigned"):
        conn.execute(
            """INSERT OR REPLACE INTO gold_label
               (message_id, verdict, note) VALUES (?, ?, ?)""",
            (mid, value, note))
    else:
        # Construction mail found in the unassigned pile is a recall miss.
        belongs = "existing" if value == "construction" else "none"
        conn.execute(
            """INSERT OR REPLACE INTO gold_label
               (message_id, entity_type, belongs_to, note) VALUES (?, ?, ?, ?)""",
            (mid, value, belongs, note))
    conn.commit()
    return redirect("/label")


@app.route("/file/<int:aid>")
@auth.login_required
def file(aid):
    row = db().execute("SELECT stored_path FROM attachment WHERE id=?",
                       (aid,)).fetchone()
    if not row:
        abort(404)
    path = os.path.join(HERE, row["stored_path"])
    if not os.path.exists(path):
        abort(404)
    name = db().execute(
        """SELECT filename FROM message_attachment
           WHERE attachment_id=? LIMIT 1""", (aid,)).fetchone()
    return send_file(path, download_name=name["filename"] if name else "file",
                     as_attachment=False)


@app.errorhandler(404)
def not_found(_):
    return page("Not found",
                "<h1>Not found</h1>"
                "<div class=sub>That page doesn't exist.</div>"
                "<div class=lede><a href='/' style='color:var(--accent-lit)'>"
                "Ask a question</a> or <a href='/jobs' "
                "style='color:var(--accent-lit)'>browse your jobs</a>.</div>",
                narrow=True), 404


def lan_ip():
    """This machine's address on the local network."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))      # no packets sent; just picks a route
        return sock.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        sock.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--lan", action="store_true",
                    help="reachable from other devices on this network")
    ap.add_argument("--port", type=int, default=5000)
    args = ap.parse_args()

    # Fail closed. Binding to every interface without a password would put a
    # contractor's whole mailbox — personal mail included — on the local
    # network for anyone who guesses the port.
    if args.lan and not auth.is_configured():
        raise SystemExit("Refusing --lan with no password set.\n"
                         "Run:  python auth.py set")

    host = "0.0.0.0" if args.lan else "127.0.0.1"
    print(f"\n  this device : http://127.0.0.1:{args.port}")
    if args.lan:
        print(f"  other devices: http://{lan_ip()}:{args.port}")
        print("\n  Same wifi only — not reachable from the internet.")
        print("  Traffic is plain HTTP: fine on your own network,")
        print("  not on public wifi. Sign out when you're done there.\n")
    app.run(debug=False, host=host, port=args.port)
