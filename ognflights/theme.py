"""Shared design system for every ognflights page.

Single source of truth for the palette, typography, radii and the shared
components (header, nav, buttons, cards, translucent overlay panels), so the
document pages (home, /my-flights, /stats) and the chrome of the Cesium pages
(/live, /replay) all read as one product.

Both generators include this:
  - ognflights/webapp.py inlines THEME_CSS into the pages it serves;
  - replay/make_replay.py inlines it into the replay TEMPLATE (which is also
    the published public page, so the CSS must stay self-contained).

Keep page-specific rules in the page; anything two pages share lives here.

Class naming: "of-" prefix (ognflights) to stay clear of Cesium's widget CSS.
  .of-body    dark page background + base type (document pages)
  .of-wrap    centred column ("narrow" modifier for form-like pages)
  .of-nav     the shared home/live/replay/my-flights/stats link row
  .of-header  centred branding block: club logo (or the soaring motif) + name
  .of-card    panel/card on document pages (a.of-card = clickable)
  .of-btn-primary    the coral call-to-action button/link
  .of-btn-secondary  teal-outline secondary action (e.g. KML download)
  .of-foot    small print
  .of-topbar  translucent pill over the 3D canvas (nav strip / day picker)
  .of-panel   translucent overlay panel over the 3D canvas (legend, settings)
  .of-helpwrap/.of-help  the first-run map-controls help overlay on /live + /replay
"""

FONT_STACK = 'system-ui,-apple-system,"Segoe UI",sans-serif'

# Cambridge Gliding Centre brand (from the club brand sheet), applied over a dark base:
#   Navy  #424D76 "established, trusted"  -> the surface tint (panels are a dark navy)
#   Coral #D97662 "warm, welcoming"       -> --accent: the primary call-to-action
#   Teal  #81D5CC "sky, fresh, forward"   -> --blue: links, highlights, active nav, focus
#   Cream #E6E5D6 "neutral"               -> body/muted text (never navy text on the dark bg)
THEME_CSS = (
    ":root{color-scheme:dark;"
    "--bg:#0e111c;--panel:#1a2032;--panel2:#232b44;--line:#333c58;"
    "--text:#f2f1e8;--dim:#b6b5a7;--faint:#6b7288;"
    "--accent:#d97662;--accent2:#c9604b;--accent-ink:#2b130c;--blue:#81d5cc;"
    "--ok:#2ecc71;--warn:#e0a33e;--bad:#e74c3c;"
    "--radius:14px;--radius-sm:9px;"
    "--overlay:rgba(14,17,28,.82);--overlay-line:rgba(151,162,197,.26);"
    "--shadow:0 6px 24px rgba(0,0,0,.25)}\n"
    ".of-body,.of-body *{box-sizing:border-box}\n"
    ".of-body{margin:0;font:16px/1.55 " + FONT_STACK + ";color:var(--text);"
    "background:var(--bg);"
    "background-image:radial-gradient(120% 42rem at 50% -12rem,#28304f 0%,rgba(14,17,28,0) 70%);"
    "background-repeat:no-repeat;min-height:100vh}\n"
    ".of-wrap{max-width:760px;margin:0 auto;padding:1.4rem 1.1rem 3rem}\n"
    ".of-wrap.narrow{max-width:640px}\n"
    ".of-nav{font-size:.85rem;color:var(--faint);margin-bottom:1.6rem;"
    "display:flex;flex-wrap:wrap;gap:.2rem 1rem}\n"
    ".of-nav a{color:var(--dim);text-decoration:none}\n"
    ".of-nav a:hover{color:var(--blue)}\n"
    ".of-nav a[aria-current]{color:var(--blue);font-weight:600}\n"
    ".of-header{text-align:center;margin-bottom:1.7rem}\n"
    # club logo: the CGC wordmark is navy-on-transparent, invisible on the dark theme;
    # brightness(0) invert(1) renders any monochrome source as a clean white silhouette.
    ".of-header .clublogo{display:block;margin:0 auto .8rem;max-height:64px;max-width:320px;"
    "filter:brightness(0) invert(1);opacity:.96}\n"
    ".of-header .soar{display:block;margin:0 auto .35rem;width:150px;height:56px}\n"
    ".of-header .club{color:var(--dim);font-size:.85rem;font-weight:600;letter-spacing:.14em;"
    "text-transform:uppercase;margin:0 0 .3rem}\n"
    ".of-header h1{font-size:2rem;letter-spacing:-.02em;margin:0 0 .45rem}\n"
    ".of-header .intro{color:var(--dim);margin:0 auto;max-width:32rem;font-size:1.02rem}\n"
    ".of-card{background:var(--panel);border:1px solid var(--line);border-radius:var(--radius);"
    "padding:1.15rem 1.2rem;box-shadow:var(--shadow)}\n"
    "a.of-card{display:block;text-decoration:none;color:inherit;"
    "transition:border-color .15s,background .15s}\n"
    "a.of-card:hover{border-color:var(--blue);background:#1f2639}\n"
    ".of-btn-primary{display:inline-block;padding:.6rem .95rem;font:inherit;font-size:.95rem;"
    "font-weight:700;color:var(--accent-ink);background:linear-gradient(180deg,#e08770,#cf6450);"
    "border:0;border-radius:10px;cursor:pointer;text-decoration:none;transition:filter .12s}\n"
    ".of-btn-primary:hover{filter:brightness(1.06)}\n"
    ".of-btn-primary:active{filter:brightness(.95)}\n"
    # secondary action (e.g. "Download for Google Earth"): teal outline, quiet next to coral
    ".of-btn-secondary{display:inline-block;padding:.55rem .9rem;font:inherit;font-size:.9rem;"
    "font-weight:600;color:var(--blue);background:transparent;border:1.5px solid var(--blue);"
    "border-radius:10px;cursor:pointer;text-decoration:none;"
    "transition:background .12s,color .12s}\n"
    ".of-btn-secondary:hover{background:rgba(129,213,204,.12)}\n"
    ".of-foot{color:var(--faint);font-size:.8rem;text-align:center;margin-top:2.2rem;"
    "line-height:1.6}\n"
    ".of-foot a{color:var(--dim)}\n"
    "input[type=checkbox],input[type=radio],input[type=range]{accent-color:var(--accent)}\n"
    # --- translucent chrome over the 3D canvas (live / replay) ---
    ".of-topbar{position:fixed;top:10px;left:50%;transform:translateX(-50%);z-index:20;"
    "display:flex;flex-wrap:nowrap;justify-content:center;align-items:center;gap:.15rem .6rem;"
    "white-space:nowrap;max-width:96vw;background:var(--overlay);border:1px solid var(--overlay-line);"
    "color:var(--text);padding:.3rem .85rem;border-radius:16px;"
    "font:13px/1.5 " + FONT_STACK + ";"
    "backdrop-filter:blur(6px);-webkit-backdrop-filter:blur(6px)}\n"
    ".of-topbar b{font-weight:700}\n"
    ".of-topbar a{color:var(--blue);text-decoration:none}\n"
    ".of-topbar a:hover{text-decoration:underline}\n"
    # wide wordmark logos need height, not a tight width cap (20px squashed the CGC mark);
    # same white-silhouette filter as the header so a navy logo reads on the dark strip.
    ".of-topbar img.nlogo{display:block;max-height:26px;max-width:160px;"
    "filter:brightness(0) invert(1);opacity:.96}\n"
    ".of-topbar .of-btn-secondary{padding:.08rem .55rem;font-size:12px;font-weight:600;"
    "border-width:1px;border-radius:8px}\n"
    ".of-topbar input[type=date],.of-topbar select{font:12px " + FONT_STACK + ";"
    "color:var(--text);background:var(--panel);border:1px solid var(--line);"
    "border-radius:6px;padding:.12rem .3rem}\n"
    ".of-panel{background:var(--overlay);border:1px solid var(--overlay-line);color:var(--text);"
    "border-radius:var(--radius-sm);box-shadow:var(--shadow);"
    "backdrop-filter:blur(6px);-webkit-backdrop-filter:blur(6px)}\n"
    ".of-panel button{appearance:none;font:12px " + FONT_STACK + ";cursor:pointer;"
    "color:var(--text);background:var(--panel2);border:1px solid var(--line);"
    "border-radius:6px;padding:.2rem .55rem}\n"
    ".of-panel button:hover{border-color:var(--dim)}\n"
    ".of-panel input[type=range]{vertical-align:middle}\n"
    # --- first-run map-controls help overlay (/live + /replay, incl. the public build) ---
    # A centred, dismissable dialog over the Cesium canvas: dimmed backdrop (click to close),
    # translucent navy card, teal bold key/mouse actions, coral "Got it". z-index sits above
    # the topbar (20) and the hover tooltip (30). [hidden] must win over display:flex.
    ".of-helpwrap{position:fixed;inset:0;z-index:50;display:flex;align-items:center;"
    "justify-content:center;padding:1rem;background:rgba(8,10,18,.55)}\n"
    ".of-helpwrap[hidden]{display:none}\n"
    ".of-help{position:relative;width:100%;max-width:430px;box-sizing:border-box;"
    "background:var(--overlay);border:1px solid var(--overlay-line);color:var(--text);"
    "border-radius:var(--radius);box-shadow:var(--shadow);padding:1.1rem 1.25rem 1.2rem;"
    "font:14px/1.55 " + FONT_STACK + ";"
    "backdrop-filter:blur(8px);-webkit-backdrop-filter:blur(8px)}\n"
    ".of-help h2{margin:0 1.6rem .55rem 0;font-size:1.08rem;letter-spacing:0}\n"
    ".of-help ul{margin:0 0 1rem;padding-left:1.15rem;color:var(--dim)}\n"
    ".of-help li{margin:.5rem 0}\n"
    ".of-help b{color:var(--blue);font-weight:600}\n"
    ".of-help-x{position:absolute;top:.4rem;right:.45rem;appearance:none;background:none;"
    "border:0;color:var(--dim);font-size:22px;line-height:1;cursor:pointer;"
    "padding:.25rem .5rem;border-radius:6px}\n"
    ".of-help-x:hover{color:var(--text)}\n"
    # small screens: let the topbar use the full width and shrink a step, so the
    # date picker + nav links stay on one line and clear of the legend panel
    "@media(max-width:640px){.of-topbar{left:8px;right:8px;transform:none;flex-wrap:wrap;"
    "max-width:none;font-size:12px;gap:.1rem .5rem;padding:.28rem .6rem}"
    ".of-topbar img.nlogo{max-height:22px;max-width:118px}}\n"
)

# --- Map-controls help overlay, shared by /live and /replay (incl. the public static
# replay). One markup + one behaviour snippet so the two views cannot drift apart.
# Markup goes in the <body> (starts hidden); the JS goes at the END of each page's
# inline script. Behaviour:
#   * auto-shows the first time a map view is opened, then never again once dismissed
#     (one shared localStorage key, MAP_HELP_KEY, because the controls are identical);
#   * X / "Got it" / Esc / clicking the backdrop all close it and set the flag;
#   * a page can veto the auto-show (the /live?demo=1 kiosk) by setting
#     window.OF_HELP_SUPPRESS=true before this snippet runs;
#   * the reopen "?" button (id=maphelpbtn, in the topbar) is wired by delegation, so
#     it works even when the topbar is injected into the HTML after the script (the
#     server-side /replay nav strip is appended just before </body>).
MAP_HELP_KEY = "ogn.maphelp.dismissed"

MAP_HELP_HTML = (
    '<div id="maphelp" class="of-helpwrap" hidden>'
    '<div class="of-help" role="dialog" aria-modal="true" aria-labelledby="maphelptitle">'
    '<button type="button" class="of-help-x" id="maphelpx" aria-label="Close help">&#215;</button>'
    '<h2 id="maphelptitle">Moving around the 3D view</h2>'
    '<ul>'
    '<li><b>Left-click and drag</b> to pan and spin the view around.</li>'
    '<li><b>Scroll the mouse wheel</b> to zoom in and out. <b>Right-click and drag</b> '
    'up or down also zooms, or pinch on a trackpad.</li>'
    '<li><b>Hold Ctrl and left-click and drag</b> (or <b>middle-click and drag</b>) '
    'to tilt the camera and change the viewing angle.</li>'
    '</ul>'
    '<button type="button" class="of-btn-primary" id="maphelpok">Got it</button>'
    '</div></div>')

MAP_HELP_JS = r"""// map-controls help overlay (shared: see ognflights/theme.py)
(function(){
  var KEY="ogn.maphelp.dismissed";
  var wrap=document.getElementById("maphelp");
  if(!wrap) return;
  function openHelp(){ wrap.hidden=false; }
  function closeHelp(){
    wrap.hidden=true;
    try{ localStorage.setItem(KEY,"1"); }catch(e){}
  }
  document.getElementById("maphelpx").addEventListener("click",closeHelp);
  document.getElementById("maphelpok").addEventListener("click",closeHelp);
  wrap.addEventListener("click",function(e){ if(e.target===wrap) closeHelp(); });
  document.addEventListener("keydown",function(e){
    if(e.key==="Escape" && !wrap.hidden) closeHelp();
  });
  // reopen via the topbar "?" button; delegated because the /replay topbar is
  // server-injected after this script in document order.
  document.addEventListener("click",function(e){
    var b=e.target.closest ? e.target.closest("#maphelpbtn") : null;
    if(b){ e.preventDefault(); openHelp(); }
  });
  var seen=false;
  try{ seen=localStorage.getItem(KEY)==="1"; }catch(e){ seen=true; }
  if(!seen && !window.OF_HELP_SUPPRESS) openHelp();
})();"""

# The topbar "?" reopen control (works after dismissal; hidden with the rest of the
# chrome in kiosk/demo mode). Include it once per page, next to the other topbar links.
MAP_HELP_BTN = ('<a href="#" id="maphelpbtn" class="of-btn-secondary" '
                'title="How to move around the 3D view">?</a>')

# Decorative soaring-arc motif: the default headline art when the club has not
# dropped a logo into <data_dir>/branding/. Shared by home + /my-flights + /stats.
SOAR_SVG = """<svg class="soar" viewBox="0 0 300 100" fill="none" aria-hidden="true">
    <path d="M18 88 Q80 84 140 62 Q196 41 232 26" stroke="#81d5cc" stroke-opacity=".55"
          stroke-width="2.5" stroke-linecap="round" stroke-dasharray="1 10"/>
    <g transform="translate(248 20) rotate(24)">
      <path d="M-46 2 Q0 -8 46 2 Q0 -16 -46 2 Z" fill="#e6e5d6"/>
      <path d="M-1.5 -7 Q0 -9 1.5 -7 Q3 2 1 14 Q0 16 -1 14 Q-3 2 -1.5 -7 Z" fill="#d97662"/>
      <path d="M-7 13 Q0 11 7 13 Q0 9 -7 13 Z" fill="#b6b5a7"/>
    </g>
  </svg>"""

# The one canonical nav: same links, same order, on every page.
NAV_LINKS = (("/", "home"), ("/live", "live"), ("/replay", "replay"),
             ("/my-flights", "my flights"), ("/stats", "stats"))


def nav_html(active=""):
    """The shared page-top nav row. `active` = the href of the current page."""
    links = "".join(
        f'<a href="{href}"{" aria-current=\"page\"" if href == active else ""}>{label}</a>'
        for href, label in NAV_LINKS)
    return f'<nav class="of-nav">{links}</nav>'


def header_html(title, intro="", logo_url="", club_name=""):
    """The shared centred branding header: club logo (or the soaring motif),
    optional club name, page title, optional intro line."""
    art = (f'<img class="clublogo" src="{logo_url}" alt="club logo">' if logo_url
           else SOAR_SVG)
    club = f'<p class="club">{club_name}</p>' if club_name else ""
    intro_html = f'<p class="intro">{intro}</p>' if intro else ""
    return f'<header class="of-header">{art}{club}<h1>{title}</h1>{intro_html}</header>'
