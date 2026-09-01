"""
JARVIS — Kivy/Pydroid3 · Groq API · Voice In (keyboard dictation) + Voice Out (TTS)
console.groq.com

WHY NOT PyAudio / pyjnius:
    PyAudio needs portaudio compiled for Android — no root, no apt, no
    compiler toolchain for that in Pydroid3, so it won't install.
    Direct Android speech APIs via pyjnius (android.speech.SpeechRecognizer)
    rely on `org.kivy.android.PythonActivity`, which only exists in apps
    built with buildozer/python-for-android — Pydroid3 hosts Python
    differently, so that class isn't guaranteed to exist here either.
    Both are unreliable on Pydroid3 specifically.

WHAT THIS USES INSTEAD (both are plain Python, nothing native to compile):
    - Voice IN:  tap the mic icon on your Android keyboard (Gboard, etc.)
                 while the text box is focused. Every Android keyboard has
                 built-in dictation — it just types the recognized text
                 straight into the box. Then hit Send (or Enter).
    - Voice OUT: gTTS turns JARVIS's reply into an mp3, played back with
                 Kivy's SoundLoader.

REQUIRED PIP INSTALLS (Pydroid3's pip GUI or its terminal):
    pip install gTTS
    (Kivy's SoundLoader ships with Pydroid3's Kivy install; if playback
    ever fails silently, `pip install ffpyplayer` gives it a codec backend)

If buildozer becomes an option later (built on a Linux machine, not on the
phone), true hands-free listening via android.speech.SpeechRecognizer +
TextToSpeech becomes possible and is more capable than this.
"""
import threading, math, json, os, tempfile, time
import urllib.request, urllib.error
from datetime import datetime
from kivy.app import App
from kivy.config import Config
Config.set('kivy', 'keyboard_mode', 'system')
Config.set('kivy', 'softinput_mode', 'below_target')
from kivy.uix.screenmanager import ScreenManager, Screen
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.floatlayout import FloatLayout
from kivy.uix.scrollview import ScrollView
from kivy.uix.label import Label
from kivy.uix.textinput import TextInput
from kivy.uix.button import Button
from kivy.uix.widget import Widget
from kivy.graphics import Color, Ellipse, Line, Rectangle, RoundedRectangle
from kivy.clock import Clock
from kivy.metrics import dp, sp
from kivy.core.window import Window
from kivy.core.audio import SoundLoader

# ── Optional voice-out dep — app still runs (silent replies) if missing ──
try:
    from gtts import gTTS
    TTS_OK = True
except Exception:
    TTS_OK = False

API_KEY = ""
MODEL   = "openai/gpt-oss-120b"
BG_DARK   = (0.05, 0.00, 0.00, 1)
BG_DARKER = (0.03, 0.00, 0.00, 1)
CARD_BG   = (0.14, 0.03, 0.03, 1)
BLUE      = (0.45, 0.04, 0.04, 1)
BG_MAIN   = (0.06, 0.06, 0.08, 1)
RED       = (0.55, 0.04, 0.04, 1)
RED_BRIGHT= (0.75, 0.08, 0.08, 1)
WHITE     = (1, 1, 1, 1)
GRAY      = (0.40, 0.20, 0.20, 1)

def greeting():
    h = datetime.now().hour
    return "Good morning sir" if h < 12 else ("Good afternoon sir" if h < 17 else "Good evening sir")

def system_prompt():
    n = datetime.now()
    return (f"You are JARVIS, Tony Stark's AI. Be concise, formal, intelligent. "
    f"you are multilingual too."
            f"Address the user respectfully. Keep replies short unless asked. "
            f"Time: {n.strftime('%I:%M %p')}, Date: {n.strftime('%A %B %d %Y')}.")

def arc_pts(cx, cy, r, a0, a1, n=80):
    pts = []
    for i in range(n + 1):
        a = math.radians(a0 + (a1 - a0) * i / n)
        pts += [cx + r * math.cos(a), cy + r * math.sin(a)]
    return pts


# ── Spinning logo widget ──────────────────────────────────────────────────────
class SpinnerLogo(Widget):
    def __init__(self, **kw):
        super().__init__(**kw)
        self.angle = 0
        self._ev = None
        self.bind(pos=self._draw, size=self._draw)

    def _draw(self, *_):
        self.canvas.clear()
        cx = self.x + self.width  / 2
        cy = self.y + self.height / 2
        R  = min(self.width, self.height) / 2
        with self.canvas:
            Color(0.15, 0.02, 0.02, 1)
            Line(points=arc_pts(cx, cy, R * 0.92, 0, 359.9), width=dp(3.5))
            Color(*RED_BRIGHT)
            a0 = self.angle
            a1 = self.angle + 260
            Line(points=arc_pts(cx, cy, R * 0.92, a0, a1, n=80), width=dp(3.5))
            Color(0.008, 0.1, 0.1, 0.1)
            r2 = R * 0.74
            Ellipse(pos=(cx - r2, cy - r2), size=(r2*2, r2*2))
            Color(*RED)
            r3 = R * 0.55
            Ellipse(pos=(cx - r3, cy - r3), size=(r3*2, r3*2))

    def start(self):
        self._ev = Clock.schedule_interval(self._tick, 1/30)

    def stop(self):
        if self._ev: self._ev.cancel()

    def _tick(self, dt):
        self.angle = (self.angle + 4) % 360
        self._draw()


# ── Loading Screen ────────────────────────────────────────────────────────────
class LoadingScreen(Screen):
    BOOT = [
        "Neural core online",
        "Language model loaded",
        "Voice output calibrated" if TTS_OK else "Voice output degraded (text-only fallback)",
        "Memory systems nominal",
        "All systems go",
    ]

    def __init__(self, **kw):
        super().__init__(**kw)
        self._idx = 0
        root = BoxLayout(orientation='vertical', padding=[0, 0, 0, dp(30)], spacing=0)
        with root.canvas.before:
            Color(*BG_MAIN)
            self._bg = Rectangle()
        root.bind(pos=self._ubg, size=self._ubg)
        root.add_widget(Widget(size_hint_y=1))
        logo_wrap = FloatLayout(size_hint=(1, None), height=dp(160))
        self.spinner = SpinnerLogo(size_hint=(None, None), size=(dp(150), dp(150)),
            pos_hint={'center_x': 0.5, 'center_y': 0.5})
        j_lbl = Label(text="J", font_size=sp(54), bold=True, color=WHITE,
            size_hint=(None, None), size=(dp(150), dp(150)),
            pos_hint={'center_x': 0.5, 'center_y': 0.5}, halign='center', valign='middle')
        j_lbl.bind(size=lambda w,s: setattr(w,'text_size',s))
        logo_wrap.add_widget(self.spinner)
        logo_wrap.add_widget(j_lbl)
        root.add_widget(logo_wrap)
        root.add_widget(Widget(size_hint_y=0.6))
        title = Label(text="JARVIS", bold=True, font_size=sp(48), color=WHITE,
            size_hint=(1, None), height=dp(70), halign='center')
        title.bind(size=lambda w,s: setattr(w,'text_size',s))
        root.add_widget(title)
        self.status = Label(text="Initializing systems...", font_size=sp(15),
            color=(0.55, 0.06, 0.06, 1), size_hint=(1, None), height=dp(36), halign='center')
        self.status.bind(size=lambda w,s: setattr(w,'text_size',s))
        root.add_widget(self.status)
        root.add_widget(Widget(size_hint_y=0.5))
        self.ok_box = BoxLayout(orientation='vertical', size_hint=(1, None),
            height=dp(130), spacing=dp(5), padding=[dp(20), 0, dp(20), 0])
        root.add_widget(self.ok_box)
        root.add_widget(Widget(size_hint_y=0.3))
        self.add_widget(root)

    def _ubg(self, w, v):
        self._bg.pos = w.pos; self._bg.size = w.size

    def on_enter(self):
        self.spinner.start()
        self._idx = 0
        self.ok_box.clear_widgets()
        Clock.schedule_once(self._next, 0.6)

    def _next(self, dt):
        if self._idx >= len(self.BOOT):
            self.status.text  = "Boot complete. Welcome, sir."
            self.status.color = RED_BRIGHT
            Clock.schedule_once(lambda dt: self._go(), 1.0)
            return
        l = Label(text=f"[ OK ]  {self.BOOT[self._idx]}", font_size=sp(13),
            color=(0.50, 0.05, 0.05, 1), size_hint=(1, None), height=dp(24), halign='center')
        l.bind(size=lambda w,s: setattr(w,'text_size',s))
        self.ok_box.add_widget(l)
        self._idx += 1
        Clock.schedule_once(self._next, 0.40)

    def _go(self):
        self.spinner.stop()
        self.manager.current = 'chat'


# ── Message Bubble ────────────────────────────────────────────────────────────
class Bubble(BoxLayout):
    def __init__(self, text, is_user=False, **kw):
        super().__init__(**kw)
        self.orientation = 'horizontal'; self.size_hint_y = None
        self.padding = [dp(10), dp(6), dp(10), dp(6)]
        mw = Window.width * 0.78
        lbl = Label(text=text, font_size=sp(14), color=WHITE,
                    size_hint=(None, None), text_size=(mw - dp(24), None),
                    halign='left', valign='top', markup=True)
        lbl.bind(texture_size=lbl.setter('size')); lbl.texture_update()
        bw = min(lbl.width + dp(24), mw); bh = lbl.height + dp(22)
        b = Widget(size_hint=(None, None), size=(bw, bh))
        b._bg = BLUE if is_user else CARD_BG
        with b.canvas.before:
            Color(*b._bg); RoundedRectangle(pos=b.pos, size=b.size, radius=[dp(14)])
        def _rd(w, *_):
            w.canvas.before.clear()
            with w.canvas.before:
                Color(*w._bg); RoundedRectangle(pos=w.pos, size=w.size, radius=[dp(14)])
        b.bind(pos=_rd, size=_rd); b.add_widget(lbl)
        b.bind(pos=lambda w,v: setattr(lbl,'pos',(w.x+dp(12),w.y+dp(10))),
               size=lambda w,v: setattr(lbl,'pos',(w.x+dp(12),w.y+dp(10))))
        if is_user: self.add_widget(Widget()); self.add_widget(b)
        else:       self.add_widget(b); self.add_widget(Widget())
        self.height = bh + dp(12)


# ── Chat Screen ───────────────────────────────────────────────────────────────
class ChatScreen(Screen):
    def __init__(self, **kw):
        super().__init__(**kw)
        self.history = []
        self._busy = False
        self._listening = False
        self._voice_mode = False   # auto-listen again after JARVIS finishes speaking
        self._sound = None

        root = BoxLayout(orientation='vertical')
        with root.canvas.before:
            Color(*BG_DARK); self._bg = Rectangle()
        root.bind(pos=self._u, size=self._u)

        hdr = BoxLayout(orientation='horizontal', size_hint=(1, None),
                        height=dp(58), padding=[dp(16), 0, dp(16), 0])
        with hdr.canvas.before:
            Color(*BG_DARKER); self._hbg = Rectangle()
        hdr.bind(pos=self._uh, size=self._uh)
        t = Label(text="JARVIS", font_size=sp(22), bold=True, color=WHITE,
                  size_hint=(None, 1), width=dp(110), halign='left')
        t.bind(size=lambda w,s: setattr(w,'text_size',s))
        self.slbl = Label(text="[color=cc1111][Online][/color]", markup=True,
                          font_size=sp(13), size_hint=(1,1), halign='right', valign='middle')
        self.slbl.bind(size=lambda w,s: setattr(w,'text_size',s))
        cb = Button(text="Clear", font_size=sp(13), size_hint=(None,None),
                    size=(dp(58),dp(34)), pos_hint={'center_y':0.5},
                    background_color=(0,0,0,0), color=(0.9,0.3,0.3,1))
        cb.bind(on_release=self._clear)
        hdr.add_widget(t); hdr.add_widget(self.slbl); hdr.add_widget(cb)
        root.add_widget(hdr)

        self.scroll = ScrollView(size_hint=(1,1), do_scroll_x=False, bar_width=0)
        self.mbox = BoxLayout(orientation='vertical', size_hint_y=None,
                              spacing=dp(4), padding=[dp(10)]*4)
        self.mbox.bind(minimum_height=self.mbox.setter('height'))
        self.scroll.add_widget(self.mbox); root.add_widget(self.scroll)

        bar = BoxLayout(orientation='horizontal', size_hint=(1,None), height=dp(62),
                        padding=[dp(12),dp(10),dp(12),dp(10)], spacing=dp(8))
        with bar.canvas.before:
            Color(*BG_DARKER); self._ibg = Rectangle()
        bar.bind(pos=self._ui, size=self._ui)

        self.inp = TextInput(hint_text="Message Jarvis...", hint_text_color=GRAY,
                             foreground_color=WHITE, background_color=(0.10,0.02,0.02,1),
                             cursor_color=(0.9,0.2,0.2,1), font_size=sp(14),
                             multiline=False, size_hint=(1,1), padding=[dp(12),dp(10)]*2)
        self.inp.bind(on_text_validate=self._send)

        # Mic button — tap to speak
        self.mic_btn = Button(text="🎙", font_size=sp(20), size_hint=(None,1), width=dp(52),
                    background_color=(0,0,0,0), color=WHITE)
        with self.mic_btn.canvas.before:
            Color(*RED); self._mbg = RoundedRectangle(radius=[dp(10)])
        self.mic_btn.bind(pos=self._um, size=self._um, on_release=self._mic_pressed)

        sb = Button(text="Send", font_size=sp(14), bold=True,
                    size_hint=(None,1), width=dp(66),
                    background_color=(0,0,0,0), color=WHITE)
        with sb.canvas.before:
            Color(*BLUE); self._sbg = RoundedRectangle(radius=[dp(10)])
        sb.bind(pos=self._us, size=self._us, on_release=self._send)

        bar.add_widget(self.inp); bar.add_widget(self.mic_btn); bar.add_widget(sb)
        root.add_widget(bar)
        self.add_widget(root)

    def _u(self,w,v):  self._bg.pos=w.pos;  self._bg.size=w.size
    def _uh(self,w,v): self._hbg.pos=w.pos; self._hbg.size=w.size
    def _ui(self,w,v): self._ibg.pos=w.pos; self._ibg.size=w.size
    def _us(self,w,v): self._sbg.pos=w.pos; self._sbg.size=w.size
    def _um(self,w,v): self._mbg.pos=w.pos; self._mbg.size=w.size

    def on_enter(self):
        if not self.history:
            msg = f"{greeting()}. I'm JARVIS."
            msg += "\nTap the mic 🎙 to open your keyboard's voice dictation, or just type."
            if not TTS_OK:
                msg += "\n(gTTS not installed — I'll reply in text only for now.)"
            self._add(msg)

    def _add(self, text, is_user=False):
        self.mbox.add_widget(Bubble(text=text, is_user=is_user))
        Clock.schedule_once(lambda dt: setattr(self.scroll,'scroll_y',0), 0.1)

    def _clear(self, *_):
        self.history.clear(); self.mbox.clear_widgets()
        self._voice_mode = False
        self._add(f"{greeting()}. Chat cleared. How can I assist?")

    # ── Voice input: hand off to the Android keyboard's own dictation ────
    def _mic_pressed(self, *_):
        # We can't capture the mic ourselves reliably in Pydroid3, so we
        # just focus the text box and open the keyboard — tap the mic icon
        # on the keyboard itself (every Android keyboard has one) to dictate.
        self._voice_mode = True   # keep speaking replies aloud for this session
        self.inp.focus = True

    def submit_dictated_text(self):
        # Called by on_text_validate when the user finishes dictating/typing
        self._send()

    # ── Text send / API call ────────────────────────────────────────────
    def _send(self, *_):
        text = self.inp.text.strip()
        if not text or self._busy: return
        self.inp.text = ''; self._add(text, is_user=True)
        self.history.append({"role":"user","content":text})
        self._busy = True; self.slbl.text = "[color=aa0808][Thinking...][/color]"
        threading.Thread(target=self._call, daemon=True).start()

    def _call(self):
        try:
            payload = json.dumps({"model":MODEL,
                "messages":[{"role":"system","content":system_prompt()}]+self.history,
                "max_tokens":512,"temperature":0.7}).encode()
            req = urllib.request.Request(
                "https://api.groq.com/openai/v1/chat/completions",
                data=payload, method="POST",
                headers={"Content-Type":"application/json",
                         "Authorization":f"Bearer {API_KEY}",
                         "User-Agent":"Mozilla/5.0 (Linux; Android 10) Chrome/120.0"})
            with urllib.request.urlopen(req, timeout=20) as r:
                reply = json.loads(r.read())['choices'][0]['message']['content'].strip()
        except urllib.error.HTTPError as e:
            reply = {401:"[Error] Invalid API key.", 429:"[Error] Rate limit.",
                     403:"[Error] Access blocked."}.get(e.code, f"[Error] HTTP {e.code}")
        except Exception as ex:
            reply = f"[Error] {str(ex)[:150]}"
        self.history.append({"role":"assistant","content":reply})
        Clock.schedule_once(lambda dt: self._done(reply), 0)

    def _done(self, reply):
        self._busy = False
        self.slbl.text = "[color=cc1111][Online][/color]"
        self._add(reply)
        if TTS_OK and self._voice_mode and reply and not reply.startswith("[Error]"):
            threading.Thread(target=self._speak, args=(reply,), daemon=True).start()

    # ── Voice output ─────────────────────────────────────────────────────
    def _speak(self, text):
        try:
            fd, path = tempfile.mkstemp(suffix=".mp3")
            os.close(fd)
            gTTS(text=text, lang="en").save(path)
            Clock.schedule_once(lambda dt: self._play(path), 0)
        except Exception:
            pass

    def _play(self, path):
        self.slbl.text = "[color=cc1111][Speaking...][/color]"
        snd = SoundLoader.load(path)
        self._sound = snd
        if not snd:
            self.slbl.text = "[color=cc1111][Online][/color]"
            return
        def _on_stop(*_):
            self.slbl.text = "[color=cc1111][Online][/color]"
            try: os.remove(path)
            except Exception: pass
        snd.bind(on_stop=_on_stop)
        snd.play()


# ── App ───────────────────────────────────────────────────────────────────────
class JarvisApp(App):
    def build(self):
        Window.clearcolor = BG_MAIN
        sm = ScreenManager()
        sm.add_widget(LoadingScreen(name='loading'))
        sm.add_widget(ChatScreen(name='chat'))
        sm.current = 'loading'
        return sm

if __name__ == '__main__':
    JarvisApp().run()
