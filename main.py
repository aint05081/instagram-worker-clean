import asyncio
import hmac
import hashlib
import html
import os
import re
from pathlib import Path
from typing import Dict, Any, Optional, List

from fastapi import FastAPI, HTTPException, Header, Query, Request
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel
from playwright.async_api import async_playwright, BrowserContext, Page, Playwright

app = FastAPI(title="Instagram Browser Worker")
PROFILE_ROOT = Path(os.getenv("PROFILE_ROOT", "/data/profiles"))
PROFILE_ROOT.mkdir(parents=True, exist_ok=True)
WORKER_API_KEY = os.getenv("WORKER_API_KEY", "")
VIEWPORT_W = int(os.getenv("VIEWPORT_W", "1280"))
VIEWPORT_H = int(os.getenv("VIEWPORT_H", "900"))

sessions: Dict[str, Dict[str, Any]] = {}
locks: Dict[str, asyncio.Lock] = {}


def valid_client_id(client_id: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_-]{8,80}", client_id or ""):
        raise HTTPException(status_code=400, detail="잘못된 client_id")
    return client_id


def require_key(x_worker_key: Optional[str], authorization: Optional[str] = None):
    if not WORKER_API_KEY:
        return
    supplied = (x_worker_key or "").strip()
    if not supplied and authorization:
        prefix = "Bearer "
        if authorization.startswith(prefix):
            supplied = authorization[len(prefix):].strip()
    if not hmac.compare_digest(supplied, WORKER_API_KEY):
        raise HTTPException(status_code=401, detail="Worker API 인증 실패")


def valid_sig(client_id: str, sig: str) -> bool:
    if not WORKER_API_KEY:
        return True
    expected = hmac.new(WORKER_API_KEY.encode(), client_id.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, sig or "")


def profile_dir(client_id: str) -> Path:
    return PROFILE_ROOT / valid_client_id(client_id)


async def cookies_logged_in(context: BrowserContext) -> bool:
    try:
        cookies = await context.cookies("https://www.instagram.com")
        return any(c.get("name") == "sessionid" and c.get("value") for c in cookies)
    except Exception:
        return False


async def open_interactive_session(client_id: str):
    client_id = valid_client_id(client_id)
    existing = sessions.get(client_id)
    if existing:
        try:
            if not existing["page"].is_closed():
                return existing
        except Exception:
            pass
        await close_session(client_id)

    pw = await async_playwright().start()
    ctx = await pw.chromium.launch_persistent_context(
        user_data_dir=str(profile_dir(client_id)),
        headless=True,
        viewport={"width": VIEWPORT_W, "height": VIEWPORT_H},
        locale="ko-KR",
        args=["--no-sandbox", "--disable-dev-shm-usage"],
    )
    page = ctx.pages[0] if ctx.pages else await ctx.new_page()
    await page.goto("https://www.instagram.com/", wait_until="domcontentloaded", timeout=60000)
    await page.wait_for_timeout(2500)
    obj = {"pw": pw, "context": ctx, "page": page}
    sessions[client_id] = obj
    return obj


async def close_session(client_id: str):
    obj = sessions.pop(client_id, None)
    if not obj:
        return
    try:
        await obj["context"].close()
    except Exception:
        pass
    try:
        await obj["pw"].stop()
    except Exception:
        pass


class ClickReq(BaseModel):
    x: float
    y: float


class TypeReq(BaseModel):
    text: str


class KeyReq(BaseModel):
    key: str


class ScrapeReq(BaseModel):
    client_id: str
    url: str
    max_rounds: int = 120


@app.get("/health")
async def health():
    return {"ok": True, "profiles": len(list(PROFILE_ROOT.glob("*")))}


@app.get("/profile/status")
async def profile_status(client_id: str, x_worker_key: Optional[str] = Header(default=None), authorization: Optional[str] = Header(default=None)):
    require_key(x_worker_key, authorization)
    client_id = valid_client_id(client_id)
    # If an interactive session is open, inspect it directly.
    if client_id in sessions:
        logged = await cookies_logged_in(sessions[client_id]["context"])
        return {"logged_in": logged, "detail": "Instagram 로그인 세션 확인 완료" if logged else "Instagram 로그인이 필요합니다."}
    # Open briefly using persistent profile to validate cookies, then close.
    lock = locks.setdefault(client_id, asyncio.Lock())
    async with lock:
        pw = await async_playwright().start()
        try:
            ctx = await pw.chromium.launch_persistent_context(
                user_data_dir=str(profile_dir(client_id)), headless=True,
                viewport={"width": VIEWPORT_W, "height": VIEWPORT_H}, locale="ko-KR",
                args=["--no-sandbox", "--disable-dev-shm-usage"],
            )
            logged = await cookies_logged_in(ctx)
            await ctx.close()
        finally:
            await pw.stop()
    return {"logged_in": logged, "detail": "Instagram 로그인 세션 확인 완료" if logged else "Instagram 로그인이 필요합니다."}


@app.get("/login", response_class=HTMLResponse)
async def login_page(client_id: str, sig: str = "", return_to: str = ""):
    valid_client_id(client_id)
    if not valid_sig(client_id, sig):
        raise HTTPException(status_code=401, detail="로그인 링크 인증 실패")
    safe_return = html.escape(return_to, quote=True)
    safe_client = html.escape(client_id, quote=True)
    safe_sig = html.escape(sig, quote=True)
    return f'''<!doctype html><html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Instagram 로그인</title><style>
body{{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;background:#f5f5f7;margin:0;color:#111}} .wrap{{max-width:1320px;margin:24px auto;padding:0 18px}}
.card{{background:#fff;border:1px solid #ddd;border-radius:18px;padding:18px;box-shadow:0 8px 30px #0000000b}} h1{{margin:0 0 8px}} .muted{{color:#666;line-height:1.5}}
#shot{{width:100%;max-width:{VIEWPORT_W}px;border:1px solid #bbb;border-radius:12px;display:block;cursor:crosshair;background:#eee}}
.controls{{display:flex;gap:8px;flex-wrap:wrap;margin:14px 0}} input{{padding:11px 12px;min-width:280px;border:1px solid #bbb;border-radius:10px}} button,a.btn{{padding:11px 14px;border:0;border-radius:10px;background:#111;color:#fff;text-decoration:none;cursor:pointer}} button.secondary{{background:#eee;color:#111}} #state{{font-weight:700;margin:10px 0}}
</style></head><body><div class="wrap"><div class="card"><h1>Instagram 로그인</h1>
<p class="muted">아래 화면은 서버에서 실행 중인 전용 브라우저입니다. 화면을 클릭해 입력 위치를 선택한 뒤, 아래 입력칸에 내용을 적고 <b>선택한 곳에 입력</b>을 누르세요. 2단계 인증도 같은 방식으로 진행할 수 있습니다. 로그인 정보는 Vercel로 전달되지 않고 이 브라우저 세션에만 사용됩니다.</p>
<div id="state">브라우저 준비 중…</div><img id="shot" alt="Instagram remote browser">
<div class="controls"><input id="text" type="password" placeholder="선택한 입력칸에 넣을 텍스트"><button id="type">선택한 곳에 입력</button><button class="secondary" id="toggle">입력값 보기</button><button class="secondary key" data-key="Tab">Tab</button><button class="secondary key" data-key="Enter">Enter</button><button class="secondary key" data-key="Backspace">Backspace</button></div>
<div class="controls"><button id="done">로그인 완료 확인</button><a class="btn" id="back" href="{safe_return or '/'}">분석 사이트로 돌아가기</a></div>
</div></div><script>
const CLIENT={safe_client!r}, SIG={safe_sig!r}; const shot=document.getElementById('shot'), state=document.getElementById('state');
async function post(path,body={{}}){{const r=await fetch(path,{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify(body)}});const d=await r.json().catch(()=>({{}}));if(!r.ok)throw new Error(d.detail||'요청 실패');return d}}
async function start(){{try{{await post(`/session/${{CLIENT}}/start?sig=${{encodeURIComponent(SIG)}}`); await refresh();}}catch(e){{state.textContent='오류: '+e.message}}}}
async function refresh(){{try{{const r=await fetch(`/session/${{CLIENT}}/screenshot?sig=${{encodeURIComponent(SIG)}}&t=${{Date.now()}}`); if(r.ok){{shot.src=URL.createObjectURL(await r.blob())}} const s=await fetch(`/session/${{CLIENT}}/status?sig=${{encodeURIComponent(SIG)}}`).then(x=>x.json()); state.textContent=s.logged_in?'✅ Instagram 로그인됨':'Instagram 로그인 진행 중 · '+(s.url||'');}}catch(e){{state.textContent='오류: '+e.message}} setTimeout(refresh,1500)}}
shot.addEventListener('click',async e=>{{const r=shot.getBoundingClientRect();const x=(e.clientX-r.left)*{VIEWPORT_W}/r.width;const y=(e.clientY-r.top)*{VIEWPORT_H}/r.height;try{{await post(`/session/${{CLIENT}}/click?sig=${{encodeURIComponent(SIG)}}`,{{x,y}})}}catch(err){{alert(err.message)}}}});
document.getElementById('type').onclick=async()=>{{const el=document.getElementById('text');try{{await post(`/session/${{CLIENT}}/type?sig=${{encodeURIComponent(SIG)}}`,{{text:el.value}});el.value='';}}catch(e){{alert(e.message)}}}};
document.getElementById('toggle').onclick=()=>{{const el=document.getElementById('text');el.type=el.type==='password'?'text':'password'}};
document.querySelectorAll('.key').forEach(b=>b.onclick=()=>post(`/session/${{CLIENT}}/key?sig=${{encodeURIComponent(SIG)}}`,{{key:b.dataset.key}}).catch(e=>alert(e.message)));
document.getElementById('done').onclick=async()=>{{const s=await fetch(`/session/${{CLIENT}}/status?sig=${{encodeURIComponent(SIG)}}`).then(x=>x.json()); if(!s.logged_in) return alert('아직 Instagram 로그인 세션이 확인되지 않았습니다.'); await post(`/session/${{CLIENT}}/close?sig=${{encodeURIComponent(SIG)}}`); location.href={safe_return!r}||'/';}};
start();</script></body></html>'''


def check_login_sig(client_id: str, sig: str):
    valid_client_id(client_id)
    if not valid_sig(client_id, sig):
        raise HTTPException(status_code=401, detail="로그인 세션 인증 실패")


@app.post("/session/{client_id}/start")
async def session_start(client_id: str, sig: str = Query(default="")):
    check_login_sig(client_id, sig)
    await open_interactive_session(client_id)
    return {"ok": True}


@app.get("/session/{client_id}/screenshot")
async def screenshot(client_id: str, sig: str = Query(default="")):
    check_login_sig(client_id, sig)
    obj = await open_interactive_session(client_id)
    png = await obj["page"].screenshot(type="png")
    return Response(content=png, media_type="image/png", headers={"Cache-Control":"no-store"})


@app.get("/session/{client_id}/status")
async def session_status(client_id: str, sig: str = Query(default="")):
    check_login_sig(client_id, sig)
    obj = await open_interactive_session(client_id)
    return {"logged_in": await cookies_logged_in(obj["context"]), "url": obj["page"].url}


@app.post("/session/{client_id}/click")
async def session_click(client_id: str, req: ClickReq, sig: str = Query(default="")):
    check_login_sig(client_id, sig)
    obj = await open_interactive_session(client_id)
    await obj["page"].mouse.click(req.x, req.y)
    await obj["page"].wait_for_timeout(250)
    return {"ok": True}


@app.post("/session/{client_id}/type")
async def session_type(client_id: str, req: TypeReq, sig: str = Query(default="")):
    check_login_sig(client_id, sig)
    obj = await open_interactive_session(client_id)
    await obj["page"].keyboard.insert_text(req.text)
    return {"ok": True}


@app.post("/session/{client_id}/key")
async def session_key(client_id: str, req: KeyReq, sig: str = Query(default="")):
    check_login_sig(client_id, sig)
    if req.key not in {"Tab", "Enter", "Backspace", "Escape"}:
        raise HTTPException(status_code=400, detail="허용되지 않은 키")
    obj = await open_interactive_session(client_id)
    await obj["page"].keyboard.press(req.key)
    return {"ok": True}


@app.post("/session/{client_id}/close")
async def session_close(client_id: str, sig: str = Query(default="")):
    check_login_sig(client_id, sig)
    await close_session(client_id)
    return {"ok": True}


def extract_shortcode(url: str) -> str:
    m = re.search(r"instagram\.com/(?:p|reel)/([^/?#]+)", url)
    if not m:
        raise HTTPException(status_code=400, detail="Instagram 게시글/Reel URL이 아닙니다.")
    return m.group(1)


def extract_comment_id(href: str) -> str:
    m = re.search(r"/c/(\d+)/", href or "")
    return m.group(1) if m else ""


async def click_more_comments(page: Page, shortcode: str, max_rounds: int = 120):
    words = ["댓글 더 보기", "댓글 더 불러오기", "이전 댓글 보기", "View more comments", "Load more comments", "View previous comments"]
    selector = f'a[href*="/p/{shortcode}/c/"], a[href*="/reel/{shortcode}/c/"]'
    best = await page.locator(selector).count(); stale = 0
    for _ in range(max(1, min(max_rounds, 250))):
        clicked = False
        nodes = page.locator("button, div[role='button'], span[role='button']")
        for i in range(min(await nodes.count(), 300)):
            n = nodes.nth(i)
            try: text = (await n.inner_text(timeout=180)).strip()
            except Exception: continue
            if text and any(w.lower() in text.lower() for w in words):
                try:
                    await n.scroll_into_view_if_needed(); await n.click(timeout=700); await page.wait_for_timeout(350); clicked=True
                except Exception: pass
        try: await page.mouse.wheel(0, 1400)
        except Exception: pass
        await page.wait_for_timeout(420)
        now = await page.locator(selector).count()
        if now > best: best=now; stale=0
        else: stale += 1
        if not clicked and stale >= 8: break


async def find_comment_blocks(page: Page, shortcode: str) -> List[Dict[str, Any]]:
    selector = f'a[href*="/p/{shortcode}/c/"], a[href*="/reel/{shortcode}/c/"]'
    links = page.locator(selector); count = await links.count(); results=[]; seen=set()
    def is_time(line):
        line=line.strip(); lower=line.lower()
        return bool(re.fullmatch(r"\d+\s*(초|분|시간|일|주|개월|달|년)(\s*전)?",line) or re.fullmatch(r"\d+[smhdwy]",lower) or re.fullmatch(r"\d{4}-\d{1,2}-\d{1,2}",line))
    def noise(line, username):
        line=line.strip(); lower=line.lower()
        if not line or line==username or is_time(line): return True
        if line in {"좋아요","답글 달기","번역 보기","팔로우","수정됨","Reply","Like","See translation","Follow","Edited"}: return True
        if "답글 보기" in line or "답글 숨기기" in line or "view replies" in lower or "hide replies" in lower: return True
        if re.fullmatch(r"좋아요\s*\d+개",line) or re.fullmatch(r"\d+\s*likes?",lower): return True
        return False
    async def username_in(container):
        anchors=container.locator('a[href]')
        for j in range(min(await anchors.count(),30)):
            a=anchors.nth(j)
            try: href=(await a.get_attribute('href') or '').strip(); txt=(await a.inner_text(timeout=180)).strip()
            except Exception: continue
            if re.fullmatch(r"/[A-Za-z0-9._]+/",href):
                first=href.strip('/')
                if first not in {"p","reel","reels","explore","direct","accounts","stories","legal","popular","web","about","developer"}: return txt or first
        return ''
    for i in range(count):
        link=links.nth(i)
        try: href=await link.get_attribute('href')
        except Exception: continue
        cid=extract_comment_id(href or '')
        if not cid or cid in seen: continue
        seen.add(cid); block=link; chosen=None
        for _ in range(12):
            try: block=block.locator('..'); text=(await block.inner_text(timeout=500)).strip()
            except Exception: break
            if not text or len(text)>2400: continue
            lines=[x.strip() for x in text.split('\n') if x.strip()]
            username=await username_in(block)
            if not username: continue
            created=next((x for x in lines if is_time(x)), '')
            likes=next((x for x in lines if re.fullmatch(r"좋아요\s*\d+개",x) or re.fullmatch(r"\d+\s*likes?",x.lower())), '')
            parts=[]
            for x in lines:
                if not noise(x,username) and x not in parts: parts.append(x)
            if parts:
                comment=' '.join(parts).strip()
                if comment and not is_time(comment): chosen=(username,comment,created,likes); break
        if chosen:
            u,c,t,l=chosen
            results.append({"comment_id":cid,"username":u,"comment":c,"created_at":t,"likes":l,"permalink":("https://www.instagram.com"+href) if href and href.startswith('/') else (href or '')})
    return results


@app.post("/scrape")
async def scrape(req: ScrapeReq, x_worker_key: Optional[str] = Header(default=None), authorization: Optional[str] = Header(default=None)):
    require_key(x_worker_key, authorization)
    client_id=valid_client_id(req.client_id); shortcode=extract_shortcode(req.url)
    if client_id in sessions:
        await close_session(client_id)
    lock=locks.setdefault(client_id,asyncio.Lock())
    async with lock:
        pw=await async_playwright().start()
        try:
            ctx=await pw.chromium.launch_persistent_context(user_data_dir=str(profile_dir(client_id)),headless=True,viewport={"width":VIEWPORT_W,"height":VIEWPORT_H},locale="ko-KR",args=["--no-sandbox","--disable-dev-shm-usage"])
            if not await cookies_logged_in(ctx):
                await ctx.close(); raise HTTPException(status_code=401,detail="Instagram 로그인이 필요합니다. 사이트의 Instagram 로그인 버튼을 눌러 먼저 로그인해 주세요.")
            page=ctx.pages[0] if ctx.pages else await ctx.new_page()
            await page.goto(req.url,wait_until='domcontentloaded',timeout=60000); await page.wait_for_timeout(3500)
            if '/accounts/login' in page.url:
                await ctx.close(); raise HTTPException(status_code=401,detail="Instagram 로그인 세션이 만료되었습니다. 다시 로그인해 주세요.")
            await click_more_comments(page,shortcode,req.max_rounds)
            comments=await find_comment_blocks(page,shortcode)
            await ctx.close()
            return {"ok":True,"count":len(comments),"comments":comments}
        finally:
            await pw.stop()
