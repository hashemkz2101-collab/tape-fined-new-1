import json, threading, http.server, os, tempfile
os.environ["APPDATA"] = tempfile.mkdtemp()
import tape_finder as tf

# --- KeyBuffer (digits only) ---
kb = tf.KeyBuffer()
for ch in "123": kb.feed(ord(ch))
assert kb.match()=="123"
kb.feed(tf.VK_BACK); assert kb.buf=="12"
kb.feed(0x66); assert kb.buf=="126"          # numpad 6
assert kb.feed(0x41)==None and kb.buf==""     # letter resets
assert kb.feed(tf.VK_RETURN) is None          # empty: no commit
kb.feed(ord("7")); assert kb.feed(tf.VK_TAB)=="commit"
assert kb.feed(0x56, ctrl=True)=="paste"
kb.set_text("۱۲"); assert kb.buf=="12"
kb.set_text("g45"); assert kb.buf=="45"
kb.set_text("abc"); assert kb.buf==""
assert tf.digits_of("B305")=="305" and tf.digits_of("12x")==""
kb.reset(); kb.feed(0x31); kb.feed(0x10); assert kb.buf=="1"   # shift ignored
for _ in range(12): kb.feed(0x32)
assert len(kb.buf)==tf.KeyBuffer.MAXLEN

# --- field classification (synthetic geometry, window at (100,50)) ---
def info(x,y,w=120,h=24,mode="uia",win=(100,50,1300,900),**kw):
    d={"proc":"order.exe","win":win,"rect":(win[0]+x,win[1]+y,win[0]+x+w,win[1]+y+h),"mode":mode,"hwnd":0,"cls":"","aid":"","names":[]}
    d.update(kw); return d
tape_f   = info(700,80)
sleeve_1 = info(650,240)
shalvar_1= info(650,480)
calib={"tape":tf.make_signature(tape_f),"sleeve":tf.make_signature(sleeve_1),"bound":tf.make_signature(shalvar_1)}
assert tf.classify(info(702,82),calib)=="tape"                 # small drift ok
assert tf.classify(info(650,240),calib)=="sleeve"
assert tf.classify(info(651,300),calib)=="sleeve"              # row 2
assert tf.classify(info(650,360),calib)=="sleeve"              # row 3
assert tf.classify(info(650,480),calib) is None                # shalvar row 1 = boundary
assert tf.classify(info(650,540),calib) is None                # below boundary
assert tf.classify(info(650,150),calib) is None                # above sleeve (e.g. model)
assert tf.classify(info(400,300),calib) is None                # other column
assert tf.classify(info(650,300,w=300),calib) is None          # wrong width
calib_nb={k:v for k,v in calib.items() if k!="bound"}
assert tf.classify(info(650,540),calib_nb)=="sleeve"           # no boundary registered
# window moved: relative coords keep working
moved=(300,200,1500,1050)
assert tf.classify(info(700,80,win=moved),calib)=="tape"
# caret mode: only y compared
cal_c={"tape":tf.make_signature(info(710,81,w=0,h=18,mode="caret"))}
assert tf.classify(info(760,83,w=0,h=18,mode="caret"),cal_c)=="tape"
assert tf.classify(info(760,200,w=0,h=18,mode="caret"),cal_c) is None
# mixed modes: compared by box center (y tol, x loose)
assert tf.classify(info(740,92,w=0,h=2,mode="caret"),calib)=="tape"
assert tf.classify(info(700,300,w=0,h=18,mode="caret"),calib)=="sleeve"
assert tf.classify(info(100,92,w=0,h=2,mode="caret"),calib) is None
# hwnd fallback when no rect
h1=dict(info(0,0),rect=None,mode="",hwnd=111); h2=dict(h1,hwnd=222)
cal_h={"tape":tf.make_signature(h1),"sleeve":tf.make_signature(h2)}
assert tf.classify(dict(h1),cal_h)=="tape" and tf.classify(dict(h2),cal_h)=="sleeve"
assert tf.classify(dict(h1,hwnd=333),cal_h) is None
# tape hwnd-only + sleeve with geometry, different hwnd
tp=dict(h1,hwnd=111); sl=dict(info(650,240),hwnd=222)
cal_m={"tape":tf.make_signature(tp),"sleeve":tf.make_signature(sl)}
assert tf.classify(dict(h1,hwnd=111),cal_m)=="tape"
assert tf.classify(dict(info(650,300),hwnd=222),cal_m)=="sleeve"
cal_same={"tape":tf.make_signature(h1),"sleeve":tf.make_signature(h1)}
assert tf.classify(dict(h1),cal_same) is None   # shared hwnd is ambiguous
# auto names
assert tf.classify(info(1,1,names=["فرم","آستین"]),{},auto_names=True)=="sleeve"
assert tf.classify(info(1,1,names=["تپه"]),{},auto_names=True)=="tape"
assert tf.classify(info(1,1,names=["آستین"]),{},auto_names=False) is None
# click mode: x approx, y with tolerance, column for sleeve
def clk(x,y,win=(100,50,1300,900)): return dict(info(x,y,w=2,h=2,mode="click",win=win))
cc={"tape":tf.make_signature(clk(700,80)),"sleeve":tf.make_signature(clk(650,240)),"bound":tf.make_signature(clk(650,480))}
assert tf.classify(clk(760,88),cc)=="tape"
assert tf.classify(clk(640,300),cc)=="sleeve" and tf.classify(clk(640,420),cc)=="sleeve"
assert tf.classify(clk(640,490),cc) is None and tf.classify(clk(400,300),cc) is None
# plausibility: whole window / big tab rejected, real field accepted
W=(343,108,1323,728)
assert not tf.plausible_field_rect(W,W)
assert not tf.plausible_field_rect((0,189,1366,706),(-8,-8,1374,736))
assert tf.plausible_field_rect((600,200,780,230),W)
# --- AutomationId (WinForms)
def ai(aid,x=0,y=0,cls="Edit",**kw): return info(x,y,aid=aid,cls=cls,**kw)
ca={"tape":tf.make_signature(ai("txtTapeCode",700,80,cls="HwndWrapper[x]")),
    "sleeve":tf.make_signature(ai("txtSleeve1",650,240)),
    "bound":tf.make_signature(ai("txtPants1",650,480))}
assert tf.classify(ai("txtTapeCode",5,900,cls="WindowsForms10.EDIT"),ca)=="tape"   # cls/position irrelevant
assert tf.classify(ai("txtSleeve3",1,2),ca)=="sleeve"
assert tf.classify(ai("txtSleeve12",300,2000),ca)=="sleeve"    # scrolled / added rows
assert tf.classify(ai("txtPants2",650,300),ca) is None          # pants never matches, even by geometry
assert tf.classify(ai("txtQty",650,300),ca)=="sleeve"          # unknown aid → geometry fallback
assert tf.aid_prefix("txtSleeve12")=="txtSleeve" and tf.aid_prefix("textBox7")=="textBox"
# generic prefix shared with tape → no aid-prefix match for sleeve
cg={"tape":tf.make_signature(ai("textBox3",700,80)),"sleeve":tf.make_signature(ai("textBox9",650,240))}
assert tf.classify(ai("textBox3",0,0),cg)=="tape"
assert tf.classify(ai("textBox12",0,0),cg) is None
# numeric aids (HWND-like) must never identify a field
cn={"tape":tf.make_signature(ai("1901268",700,80)),"sleeve":tf.make_signature(ai("1901268",650,240))}
assert cn["tape"]["aid"]=="" and not tf.usable_aid("1901268") and tf.usable_aid("txtA")
assert tf.classify(ai("1901268",650,300),cn)=="sleeve"      # geometry decides, not the shared aid
assert tf.classify(ai("1901268",702,82),cn)=="tape"
print("keybuf+classify OK")

# --- API with mock server ---
STAR=[{"tape":"G12","box":"5","status":"تپه در باکس موجود است","holderUsername":""},
      {"tape":"B305","box":"","status":"در حال استفاده","holderUsername":"ali"}]
MORV=[{"tape":"g12","box":"9","status":"تپه در باکس موجود است","holderUsername":""}]
class H(http.server.BaseHTTPRequestHandler):
    def log_message(self,*a): pass
    def do_POST(self):
        act=self.path.split("action=")[1]
        n=int(self.headers.get("Content-Length",0)); body=json.loads(self.rfile.read(n) or b"{}")
        def send(code,obj):
            b=json.dumps(obj,ensure_ascii=False).encode(); self.send_response(code)
            self.send_header("Content-Type","application/json"); self.end_headers(); self.wfile.write(b)
        auth=self.headers.get("Authorization","")
        if act=="login":
            return send(200,{"ok":True,"token":"T1","remember_token":"R1"}) if body["password"]=="pw" else send(401,{"ok":False,"error":"bad"})
        if act=="loginWithRememberToken":
            return send(200,{"ok":True,"token":"T2"}) if body["remember_token"]=="R1" else send(401,{"ok":False,"error":"x"})
        if auth not in ("Bearer T1","Bearer T2"): return send(401,{"ok":False,"error":"expired"})
        if act=="getInitialData": return send(200,{"ok":True,"rows":STAR})
        if act=="getInitialData2": return send(403,{"ok":False,"error":"no access"}) if os.environ.get("DENY2") else send(200,{"ok":True,"rows":MORV})
srv=http.server.HTTPServer(("127.0.0.1",0),H); threading.Thread(target=srv.serve_forever,daemon=True).start()
cfg=dict(tf.DEFAULT_CONFIG); cfg["base_url"]="http://127.0.0.1:%d/index.php"%srv.server_port
api=tf.Api(cfg); api.login("u","pw"); assert cfg["remember_token"]=="R1"
idx=tf.TapeIndex(); assert idx.refresh(api)=={}
print(idx.counts())
f=idx.search("g12"); print(tf.describe("G12",f,{},"تپه"))
assert [x[0] for x in f]==["star","morvarid"]
assert idx.search("G012")==f
print(tf.describe("B305",idx.search("B305"),{}))
print(tf.describe("G99",idx.search("G99"),{}))
# expired session -> auto relogin
api.token="stale"; assert idx.refresh(api)=={}
# one source denied
os.environ["DENY2"]="1"; errs=idx.refresh(api); print(errs)
print(tf.describe("G99",idx.search("G99"),errs))
# wrong password
try: tf.Api(cfg).login("u","bad")
except tf.ApiError as e: print("ok err:",e)
# --- canon / fallback both ---
assert tf.canon("G-012")=="G12" and tf.canon(" g12 ")=="G12" and tf.canon("۱۲")=="12" and tf.canon("B0305")=="B305"
os.environ.pop("DENY2",None); api.token=""; idx.refresh(api)
assert idx.search("G0012") and idx.search("b305")
t,l,c=tf.describe_both("305",idx,{},"note"); print(t,l); assert "موجود است" in t and c=="#1b7f3b" and any("آستین B305: موجود" in x for x in l)
t,l,c=tf.describe_both("999",idx,{}); assert "در هیچ" in t and c=="#a12622"
assert "معرفی نشده" in tf.why_unknown({"mode":"caret"},{})
assert "فرق کرد" in tf.why_unknown({"mode":"caret"},{"tape":{"mode":"uia"}})
tf.debug_log("test"); assert os.path.exists(os.path.join(tf.APP_DIR,"debug.log"))
print("ALL OK")
