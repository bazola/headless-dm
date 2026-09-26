"""voices.py - a character's voice, cast once from words and kept forever (plan 57).

The flow the Journey page drives through the lore gate:

  who(guid)          what the caster needs to know: race, class, sex, temperament, personality
  suggest(guid)      a local model writes a voice description from those; the player may rewrite it
  sample(guid, ...)  Fish designs a voice from the description and says a line with it -> a candidate
  accept(guid, id)   the candidate becomes the character's reference clip. LOCKED from here on
  line(guid, text)   any later line is a clone of that clip, cached forever by (model, clip, text)

Why Fish S2.1 Pro through OpenRouter, and not the Qwen TTS the operator first named (measured 2026-09-26):
qwen/qwen-audio-3.0-tts-plus has two fixed voices and returns byte-identical audio whatever style it is
given, so it cannot give 1,283 people voices of their own. Fish follows a bracketed description
("[a very deep, gravelly old man's voice]", 87 Hz; "[a high, squeaky young girl's voice]", 296 Hz) without
speaking it, and clones from a reference clip (87 Hz in, 90 Hz out, twice). Designing and cloning with one
model is what makes "locked" mean something: the voice lives in our clip, not in a sampler's mood.

Audio is on disk. The reference WAVs sit in DATA_DIR/voices (never served); what the page plays sits in
dashboard-data/voices, which the dashboard serves at /data with no token, like the journey stories.
"""
import base64
import hashlib
import io
import json
import os
import re
import threading
import time
import urllib.error
import urllib.request
import wave

import fleet
import gen_backstories as g

HERE = os.path.dirname(os.path.realpath(__file__))
DATA_DIR = g.site.get("DATA_DIR", "/opt/wow/server/data")
REF_DIR = os.path.join(DATA_DIR, "voices")                          # reference clips, private
WEB_ROOT = os.path.join(DATA_DIR, "dashboard-data")
WEB_DIR = os.path.join(WEB_ROOT, "voices")                          # what the page plays
SAMPLE_DIR = os.path.join(WEB_DIR, "samples")
LINE_DIR = os.path.join(WEB_DIR, "lines")
CAST_DIR = os.path.join(WEB_DIR, "cast")

MODEL = g.site.get("VOICE_MODEL", "fish-audio/s2.1-pro")
KEY_ENV = g.site.get("VOICE_KEY_ENV", "OPENROUTER_API_KEY")
URL = g.site.get("VOICE_URL", "https://openrouter.ai/api/v1/audio/speech")
DEFAULT_LANE = "evo-quality"
MAX_LINE_CHARS = 600         # one bubble; the key is capped at $50, and a runaway paste should not spend it
SAMPLE_KEPT_SECONDS = 86400  # an unaccepted sample is swept a day later
# Fish does not reliably honour the sex in a description (2026-09-26): one "low, quiet woman's voice" came
# back at 100, 176 and 200 Hz on three takes, and 4 of 5 women's first samples sat at 92-145 Hz, where a
# listener hears a man. Rewording moved nothing past the noise. So every sample is measured, and a take
# whose pitch does not fit the character's sex is made again. Speaking pitch: most men 85-180 Hz, most
# women 165-255 Hz.
SEX_PITCH = {1: (160, 400), 0: (60, 180)}
SEX_TRIES = 6
TIMEOUT = 90

GENDERS = {0: "man", 1: "woman"}
SAMPLE_LINES = (
    "Stay close. The road bends ahead, and I have no wish to lose anyone else before nightfall.",
    "I have walked a long way to stand here, and I will walk further still before this is done.",
    "Keep your blade close and your wits closer. Nothing out here forgives a careless step.",
)

_locks = {}
_locks_guard = threading.Lock()


def _lock(guid):
    with _locks_guard:
        return _locks.setdefault(int(guid), threading.Lock())


def ensure_schema():
    g.sql(open(os.path.join(HERE, "voice_tables.sql")).read(), fetch=False)
    for d in (REF_DIR, SAMPLE_DIR, LINE_DIR, CAST_DIR):
        os.makedirs(d, exist_ok=True)
    # The line exactly as the record holds it, so the page can find its audio without the token. Rows
    # rendered before this column existed are recovered from the ledger by what they sound like.
    if not g.sql("SELECT 1 FROM information_schema.columns WHERE table_schema = DATABASE() "
                 "AND table_name = 'voice_line' AND column_name = 'raw_text'"):
        g.sql("ALTER TABLE voice_line ADD COLUMN raw_text VARCHAR(1000) NULL "
              "COMMENT 'The line as the journey file holds it; what the page looks it up by' AFTER text",
              fetch=False)
    for key, guid, said in g.sql("SELECT cache_key, guid, " + g._flat("text") + " FROM voice_line "
                                 "WHERE raw_text IS NULL"):
        for (raw,) in g.sql("SELECT " + g._flat("text") + f" FROM ledger_chat WHERE speaker_guid = {int(guid)} "
                            f"AND text LIKE {g.q('%' + said[:40].replace('%', '').replace('_', '') + '%')}"):
            if speakable(raw) == said:
                g.sql(f"UPDATE voice_line SET raw_text = {_exact(raw)} WHERE cache_key = '{key}'", fetch=False)
                break
    publish()


def _exact(text):
    """A SQL string kept byte for byte. g.q folds smart punctuation to ASCII for the game client, and the
    raw line is only useful if it matches the journey file exactly."""
    return "'" + text.replace("\\", "\\\\").replace("'", "\\'") + "'"


INDEX = os.path.join(WEB_DIR, "index.json")
_index_lock = threading.Lock()


def publish():
    """What the page reads, with no token: who has a voice, and every line said aloud, by the words the
    journey file holds. The page has no way to compute a cache key (crypto.subtle is refused over plain
    http off localhost), so it looks lines up by their text instead."""
    doc = dict(generated=int(time.time()), enabled=enabled(), voices={}, lines={})
    for guid, name in g.sql("SELECT guid, name FROM character_voice"):
        doc["voices"][guid] = name
    for guid, raw, said, rel in g.sql("SELECT guid, " + g._flat("raw_text") + ", " + g._flat("text")
                                      + ", file FROM voice_line ORDER BY created_at"):
        doc["lines"].setdefault(guid, {})[raw or said] = "data/" + rel
    with _index_lock:
        _write(INDEX, json.dumps(doc, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))
    return doc


class VoiceError(Exception):
    """Something the player should be told, in words, with an HTTP status to send it under."""
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


# ---------------------------------------------------------------------------
# who is being cast

def resolve(guid=None, name=None):
    """A guid from either: talk bubbles carry a guid, bot_talk encounters only a name."""
    if guid is not None and str(guid).isdigit():
        return int(guid)
    if name and re.fullmatch(r"[^\s'\"\\;]{2,12}", str(name)):
        rows = g.sql(f"SELECT guid FROM characters WHERE name = {g.q(str(name))}")
        if rows:
            return int(rows[0][0])
    raise VoiceError(f"There is nobody called {name or guid} to give a voice to.", 404)


def who(guid):
    rows = g.sql("SELECT c.name, c.race, c.class, c.gender, c.level, IFNULL(lc.temperament, ''), "
                 f"{g._flat('lc.personality')}, {g._flat('lc.gist')} FROM characters c "
                 f"LEFT JOIN lore_character lc ON lc.guid = c.guid WHERE c.guid = {int(guid)}")
    if not rows:
        raise VoiceError("That character is not in the world's books.", 404)
    r = rows[0]
    gender = int(r[3])
    return dict(guid=int(guid), name=r[0], race=g.RACES.get(int(r[1]), "unknown"),
                cls=g.CLASSES.get(int(r[2]), "adventurer"), gender=gender,
                sex=GENDERS.get(gender, "person"), level=int(r[4]),
                temperament=(r[5] or "").lower().replace("_", " "), personality=r[6] or "", gist=r[7] or "")


def voice_of(guid):
    rows = g.sql("SELECT name, gender, " + g._flat("style") + ", " + g._flat("sample_text")
                 + f", ref_file, model, UNIX_TIMESTAMP(created_at) FROM character_voice WHERE guid = {int(guid)}")
    if not rows:
        return None
    r = rows[0]
    return dict(guid=int(guid), name=r[0], gender=int(r[1]), style=r[2], sample_text=r[3],
                ref_file=r[4], model=r[5], created=int(r[6]), locked=True,
                sample_url=f"data/voices/cast/{int(guid)}.wav")


def state(guid):
    """What the page opens with: the voice if there is one, and always who they are."""
    return dict(who=who(guid), voice=voice_of(guid), model=MODEL, sample_text=sample_text(guid), enabled=enabled())


# ---------------------------------------------------------------------------
# the description

# The voice designer hears "low", "deep", "gravelly" and the like as a man's voice whatever else the line
# says: tauren women described "low and resonant" came back at 72-138 Hz on four takes out of four. The same
# weight survives in words it keeps feminine.
WOMAN_WORDS = ("The voice designer hears some words as a man whatever else the line says, so for her NEVER use: low, "
               "deep, gravelly, gravel, rumbling, thunderous, booming, bass, husky, weathered, rasping, raspy, gritty, "
               "rough. Give age, weight and hardness in other ways, picking what fits her, not a set list: "
               "e.g. clipped, dry, cool, tired, flinty, measured, smoky, rich, breathy, bright, lilting, sharp, soft.\n")


def suggest(guid):
    """A voice description written from who they are. A suggestion only: the player edits it."""
    w = who(guid)
    prompt = f"""You are casting a voice actor for {w['name']}, a {w['race']} {w['cls']} and a {w['sex']}, who lives in Azeroth.
{f"Their manner: {w['temperament']}." if w['temperament'] else ""}
{f"Who they are: {w['personality']}" if w['personality'] else ""}
{f"Their past, in brief: {w['gist']}" if w['gist'] else ""}

Describe ONLY how their speaking voice sounds, for a voice designer: whether it is a {w['sex']}'s voice, rough age,
pitch, texture (gravelly, breathy, clear, rasping, smooth), accent or lilt fitting a {w['race']}, pace and manner.
Only the sound: nothing about hands, eyes, gestures, clothes or what they do.
{WOMAN_WORDS if w['gender'] == 1 else ""}One line, 12 to 30 words, no name, no quotation marks, no brackets, no story. Start with exactly "A {w['sex']}'s voice,".
Example (for someone else): A man's voice, weathered, deep and gravelly, with a slow rolling brogue and a dry, amused edge."""
    lane = fleet.prefer_lanes(DEFAULT_LANE)[0]
    last = None
    for _ in range(3):
        try:
            text = lane.chat([{"role": "user", "content": prompt}], 120, temperature=0.9)
        except RuntimeError as err:
            last = err
            continue
        text = _trim(clean_style(text.splitlines()[0] if text else ""), 32)
        if 6 <= len(text.split()) <= 34:
            return text
        last = f"it came back as {len(text.split())} words"
    raise VoiceError(f"No description came back ({last}). Write one yourself.", 502)


def _trim(text, most):
    """The local model overshoots a word cap more often than not (39-43 words, three runs in three). A
    description is a list of qualities, so cutting it at the last comma inside the cap loses the tail
    and nothing else."""
    words = text.split()
    if len(words) <= most:
        return text
    cut = " ".join(words[:most])
    return (cut[:cut.rfind(",")] if "," in cut else cut).rstrip(" ,;-") + "."


def clean_style(text):
    """A description goes inside [ ], so it may hold no brackets of its own and no quotes."""
    text = re.sub(r"[\[\]{}\"“”]", "", str(text)).strip().strip("'").strip()
    text = re.sub(r"^(voice|description)\s*:\s*", "", text, flags=re.I)
    return re.sub(r"\s+", " ", text)[:380]


def sample_text(guid):
    """The line the sample says: something they have really said, when there is a good one."""
    rows = g.sql("SELECT REPLACE(REPLACE(text, '\\t', ' '), '\\n', ' ') FROM ledger_chat "
                 f"WHERE speaker_guid = {int(guid)} AND CHAR_LENGTH(text) BETWEEN 70 AND 220 "
                 "ORDER BY id DESC LIMIT 20")
    for r in rows:
        said = speakable(r[0])
        if 60 <= len(said) <= 220 and not re.search(r"\d", said):
            return said
    return SAMPLE_LINES[int(guid) % len(SAMPLE_LINES)]


# ---------------------------------------------------------------------------
# text the ear should hear

_LINK = re.compile(r"\|H[^|]*\|h\[?([^\]|]*)\]?\|h")      # |Hitem:...|h[Name]|h -> Name
_COLOUR = re.compile(r"\|c[0-9a-fA-F]{8}|\|r")


def speakable(text):
    """What a line sounds like said aloud. Square brackets would be read by Fish as directions, so an
    item's [Name] must lose them; *emotes* are actions, not words."""
    t = _LINK.sub(r"\1", str(text))
    t = _COLOUR.sub("", t)
    t = re.sub(r"\*[^*]{1,80}\*", " ", t)
    t = re.sub(r"[\[\]{}<>]", "", t)
    return re.sub(r"\s+", " ", t).strip()


# ---------------------------------------------------------------------------
# the engine

_broken = None


def disable(reason):
    """Voices could not be set up. Say so to the page too, so an index left from an earlier run does not
    keep offering speakers that would only fail."""
    global _broken
    _broken = reason
    try:
        _write(INDEX, json.dumps(dict(generated=int(time.time()), enabled=False, voices={}, lines={})).encode())
    except OSError:
        pass


def off_reason():
    """Why voices are off on this realm, or None when they are on. Voices are optional: a realm that never
    sets a speech key, or sets VOICE_ENABLED=0, gets no speakers on the page and no voice doors on the gate,
    and nothing else changes. Read once at start, like every other gate setting: restart the gate after
    changing either."""
    if _broken:
        return f"Voices could not be set up on this realm: {_broken}"
    if str(g.site.get("VOICE_ENABLED", "1")).strip().lower() in ("0", "off", "no", "false"):
        return "Voices are switched off on this realm (VOICE_ENABLED=0)."
    if not g.site.get(KEY_ENV, ""):
        return f"Voices are off: there is no speech key ({KEY_ENV} in site/secrets.env)."
    return None


def enabled():
    return off_reason() is None


def _key():
    key = g.site.get(KEY_ENV, "")
    if not key:
        raise VoiceError(off_reason() or f"There is no speech key: set {KEY_ENV} in site/secrets.env.", 503)
    return key


def _speak(body):
    """One call to the speech endpoint. Returns (bytes, content-type)."""
    body = dict(model=MODEL, **body)
    req = urllib.request.Request(URL, json.dumps(body).encode(),
                                 {"Authorization": f"Bearer {_key()}", "Content-Type": "application/json"})
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            data, ctype = r.read(), r.headers.get("Content-Type", "")
    except urllib.error.HTTPError as err:
        detail = err.read()[:300].decode("utf-8", "replace")
        g.log(f"voices: {MODEL} HTTP {err.code}: {detail}")
        raise VoiceError(f"The voice could not be made (HTTP {err.code}).", 502) from None
    except (urllib.error.URLError, OSError) as err:
        raise VoiceError(f"The voice service could not be reached: {err}", 502) from None
    g.log(f"voices: {MODEL} {len(body.get('input', ''))} chars -> {len(data)} bytes in {time.time() - t0:.1f}s")
    if not data:
        raise VoiceError("The voice came back silent.", 502)
    return data, ctype


def _wav(pcm, ctype):
    """Raw PCM as a WAV, at the rate the header names (Fish says audio/pcm;rate=44100;channels=1)."""
    rate = int((re.search(r"rate=(\d+)", ctype) or [0, 44100])[1])
    channels = int((re.search(r"channels=(\d+)", ctype) or [0, 1])[1])
    out = io.BytesIO()
    with wave.open(out, "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)
    return out.getvalue()


def pitch(pcm, rate):
    """Median speaking pitch in Hz of 16-bit mono PCM, 0 when nothing voiced was found.

    Autocorrelation on 40 ms frames, taking the FIRST lag whose correlation is within 85% of the best:
    the plain arg-max locks onto twice the period and halves the pitch, which reads a woman as a man --
    exactly the error this exists to catch. Checked against known clips: a "deep old man" 88 Hz, a
    "squeaky young girl" 333 Hz. Pure Python (no numpy on the realm box); ~0.3 s for a sample."""
    import array
    import statistics
    x = array.array("h")
    x.frombytes(pcm[: len(pcm) // 2 * 2])
    step = max(1, rate // 8000)                  # ~8 kHz, box-averaged so the decimation does not alias
    x = [sum(x[i:i + step]) // step for i in range(0, len(x) - step, step)]
    r = rate // step
    fr, lo, hi = int(r * 0.04), r // 400, r // 60
    found = []
    for i in range(0, len(x) - fr * 2, fr):
        w = x[i:i + fr * 2]
        energy = sum(v * v for v in w[:fr]) / fr
        if energy < 800 ** 2:
            continue
        ac = [sum(w[j] * w[j + k] for j in range(fr)) for k in range(lo, hi)]
        best = max(ac)
        if best <= 0.3 * energy * fr:
            continue
        for n, c in enumerate(ac):
            if c >= 0.85 * best and (n == 0 or c >= ac[n - 1]) and (n + 1 == len(ac) or c >= ac[n + 1]):
                found.append(r / (lo + n))
                break
    return round(statistics.median(found)) if found else 0


def _write(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path + ".tmp", "wb") as f:
        f.write(data)
    os.replace(path + ".tmp", path)


# ---------------------------------------------------------------------------
# casting

def _sweep_samples():
    now = time.time()
    for f in os.listdir(SAMPLE_DIR):
        p = os.path.join(SAMPLE_DIR, f)
        try:
            if now - os.path.getmtime(p) > SAMPLE_KEPT_SECONDS:
                os.remove(p)
        except OSError:
            pass


def sample(guid, style, text=None):
    """Design a voice from words and let it speak. Stores nothing but a candidate file."""
    guid = int(guid)
    if voice_of(guid):
        raise VoiceError("They already have a voice, and it stays.", 409)
    style = clean_style(style)
    if len(style.split()) < 3:
        raise VoiceError("Describe the voice in a few words first.")
    said = speakable(text) if text else sample_text(guid)
    if not 10 <= len(said) <= 300:
        raise VoiceError("The sample line should be a sentence or two.")
    w = who(guid)
    low, high = SEX_PITCH.get(w["gender"], (0, 1000))
    with _lock(guid):
        best = None
        for take in range(1, SEX_TRIES + 1):
            pcm, ctype = _speak(dict(input=f"[{style}] {said}", response_format="pcm"))
            hz = pitch(pcm, int((re.search(r"rate=(\d+)", ctype) or [0, 44100])[1]))
            miss = 0 if low <= hz <= high else min(abs(hz - low), abs(hz - high))
            g.log(f"voices: {w['name']} ({w['sex']}) take {take}: {hz} Hz" + ("" if not miss else " - does not fit"))
            if best is None or miss < best[0]:
                best = (miss, pcm, ctype, hz, take)
            if not miss:
                break
        miss, pcm, ctype, hz, _ = best
        wav = _wav(pcm, ctype)
        _sweep_samples()
        sid = f"{guid}-{int(time.time() * 1000)}"
        _write(os.path.join(SAMPLE_DIR, sid + ".wav"), wav)
        _write(os.path.join(SAMPLE_DIR, sid + ".json"), json.dumps(dict(style=style, text=said)).encode())
    fits = not miss
    return dict(sample=sid, url=f"data/voices/samples/{sid}.wav", style=style, text=said, pitch=hz, takes=take,
                fits=fits, warning=None if fits else
                f"After {take} tries this still sounds more like a {'man' if w['gender'] == 1 else 'woman'}'s voice "
                f"than a {w['sex']}'s ({hz} Hz). Try again, or change the description.")


def decline(guid, sid):
    for ext in (".wav", ".json"):
        p = os.path.join(SAMPLE_DIR, _sample_id(guid, sid) + ext)
        if os.path.exists(p):
            os.remove(p)


def _sample_id(guid, sid):
    sid = str(sid)
    if not re.fullmatch(rf"{int(guid)}-\d+", sid):
        raise VoiceError("That is not one of their samples.")
    return sid


def accept(guid, sid):
    """The sample becomes the voice. From here every line they say is a clone of this clip."""
    guid = int(guid)
    sid = _sample_id(guid, sid)
    src = os.path.join(SAMPLE_DIR, sid + ".wav")
    meta = os.path.join(SAMPLE_DIR, sid + ".json")
    if not (os.path.exists(src) and os.path.exists(meta)):
        raise VoiceError("That sample is gone; make another.", 410)
    w = who(guid)
    with _lock(guid):
        if voice_of(guid):
            raise VoiceError("They already have a voice, and it stays.", 409)
        info = json.load(open(meta))
        data = open(src, "rb").read()
        ref = f"{guid}.wav"
        _write(os.path.join(REF_DIR, ref), data)
        _write(os.path.join(CAST_DIR, ref), data)
        g.sql("INSERT INTO character_voice (guid, name, gender, style, sample_text, ref_file, ref_sha1, model) "
              f"VALUES ({guid}, {g.q(w['name'])}, {w['gender']}, {g.q(info['style'])}, {g.q(info['text'])}, "
              f"{g.q(ref)}, '{hashlib.sha1(data).hexdigest()}', {g.q(MODEL)})", fetch=False)
        decline(guid, sid)
    g.log(f"voices: {w['name']} ({guid}) cast as: {info['style']}")
    publish()
    return voice_of(guid)


# ---------------------------------------------------------------------------
# speaking

def line(guid, text):
    """One line in their locked voice. Paid for once; afterwards it is a file on disk."""
    guid = int(guid)
    v = voice_of(guid)
    if not v:
        raise VoiceError("They have no voice yet.", 409)
    said = speakable(text)
    if not said:
        raise VoiceError("There is nothing in that line to say aloud.")
    if len(said) > MAX_LINE_CHARS:
        raise VoiceError(f"That is too long to say in one breath ({len(said)} characters).")
    ref_path = os.path.join(REF_DIR, v["ref_file"])
    ref = open(ref_path, "rb").read()
    key = hashlib.sha1(f"{MODEL}\n{hashlib.sha1(ref).hexdigest()}\n{said}".encode()).hexdigest()
    rel = f"voices/lines/{key}.mp3"
    path = os.path.join(WEB_ROOT, rel)
    if os.path.exists(path):
        return dict(url="data/" + rel, cached=True, text=said, guid=guid)
    with _lock(guid):
        if not os.path.exists(path):
            data, _ = _speak(dict(input=said, response_format="mp3", input_references=[
                {"type": "input_audio", "input_audio": {"data": base64.b64encode(ref).decode(), "format": "wav"}},
                {"type": "text", "text": v["sample_text"]}]))
            _write(path, data)
            g.sql("INSERT IGNORE INTO voice_line (cache_key, guid, text, raw_text, file, chars) VALUES "
                  f"('{key}', {guid}, {g.q(said[:1000])}, {_exact(str(text)[:1000])}, {g.q(rel)}, {len(said)})",
                  fetch=False)
            publish()
    return dict(url="data/" + rel, cached=False, text=said, guid=guid)


# ---------------------------------------------------------------------------
# a section, for a video editor

EXPORT_DIR = os.path.join(WEB_DIR, "exports")
EXPORT_KEPT_SECONDS = 86400
RATE = 44100                 # every Fish line comes back 44.1 kHz mono
GAP_CAP = 8.0                # "as it happened": a quiet minute must not become a minute of silence
GAP_MIN = 0.3                # lines said in the same second still get a breath between them
BACK_TO_BACK = 0.6


def _decode(path):
    import subprocess
    out = subprocess.run(["ffmpeg", "-v", "error", "-i", path, "-f", "s16le", "-ac", "1", "-ar", str(RATE), "-"],
                         capture_output=True)
    if out.returncode != 0:
        raise VoiceError(f"A line could not be read back: {os.path.basename(path)}", 500)
    return out.stdout


def _srt_time(t):
    ms = int(round(t * 1000))
    return f"{ms // 3600000:02d}:{ms // 60000 % 60:02d}:{ms // 1000 % 60:02d},{ms % 1000:03d}"


def _mix(clips, starts):
    """One mono track with each clip placed at its start, in seconds. Returns the MP3 bytes."""
    import subprocess
    end = max(s + len(c) / 2 / RATE for c, s in zip(clips, starts)) + 0.5
    track = bytearray(int(end * RATE) * 2)
    for clip, start in zip(clips, starts):
        at = int(start * RATE) * 2
        track[at:at + len(clip)] = clip
    out = subprocess.run(["ffmpeg", "-v", "error", "-f", "s16le", "-ar", str(RATE), "-ac", "1", "-i", "-",
                          "-b:a", "192k", "-f", "mp3", "-"], input=bytes(track), capture_output=True)
    if out.returncode != 0:
        raise VoiceError("The track could not be put together.", 500)
    return out.stdout


def export(lines, title):
    """Every recorded line of a section as a zip: the lines one by one, two mixed tracks (as it happened,
    and back to back), a subtitle file for each, and a manifest. `lines` are {guid|name, text, ts} in
    the order they were said. Only recorded lines go in; the rest are named in the manifest as missing."""
    import shutil
    import zipfile
    if not shutil.which("ffmpeg"):
        raise VoiceError("Packing a section needs ffmpeg, and this server has none. Install it and try again.", 503)
    os.makedirs(EXPORT_DIR, exist_ok=True)
    now = time.time()
    for f in os.listdir(EXPORT_DIR):
        p = os.path.join(EXPORT_DIR, f)
        if now - os.path.getmtime(p) > EXPORT_KEPT_SECONDS:
            os.remove(p)

    names, recorded = {}, {}
    for guid, name in g.sql("SELECT guid, name FROM characters WHERE guid IN (SELECT guid FROM voice_line)"):
        names[int(guid)] = name
    for guid, raw, rel in g.sql("SELECT guid, " + g._flat("raw_text") + ", file FROM voice_line"):
        recorded[(int(guid), raw)] = rel

    items, missing = [], []
    for l in lines[:2000]:
        try:
            guid = resolve(l.get("guid"), l.get("name"))
        except VoiceError:
            missing.append(dict(name=l.get("name"), text=l.get("text")))
            continue
        rel = recorded.get((guid, str(l.get("text", "")).replace("\n", " ").replace("\t", " ")))
        if not rel:
            missing.append(dict(name=names.get(guid) or l.get("name"), text=l.get("text")))
            continue
        items.append(dict(guid=guid, name=names.get(guid) or l.get("name") or str(guid), text=l["text"],
                          ts=float(l.get("ts") or 0), path=os.path.join(WEB_ROOT, rel)))
    if not items:
        raise VoiceError("Nothing in this section has been recorded yet.", 409)

    clips = [_decode(i["path"]) for i in items]
    durs = [len(c) / 2 / RATE for c in clips]
    real, packed, t_real, t_pack = [], [], 0.0, 0.0
    first = items[0]["ts"]
    for n, (i, d) in enumerate(zip(items, durs)):
        if n:
            gap = min(GAP_CAP, max(0.0, i["ts"] - items[n - 1]["ts"] - durs[n - 1]))
            t_real = max(real[-1] + durs[n - 1] + GAP_MIN, real[-1] + durs[n - 1] + gap)
            t_pack = packed[-1] + durs[n - 1] + BACK_TO_BACK
        real.append(t_real)
        packed.append(t_pack)

    safe = re.sub(r"[^A-Za-z0-9_-]+", "-", str(title or "section")).strip("-")[:60] or "section"
    stamp = time.strftime("%Y-%m-%d_%H-%M", time.localtime(first)) if first else time.strftime("%Y-%m-%d")
    base = f"{safe}_{stamp}"
    zpath = os.path.join(EXPORT_DIR, base + ".zip")
    manifest = dict(title=title, exported=int(now), first_ts=first, lines=[], missing=missing,
                    note="ts is whole seconds from the game's record; nudge lines in the editor to match video.")
    with zipfile.ZipFile(zpath + ".tmp", "w", zipfile.ZIP_STORED) as z:
        srt_real, srt_pack = [], []
        for n, (i, d) in enumerate(zip(items, durs), 1):
            when = time.strftime("%H-%M-%S", time.localtime(i["ts"])) if i["ts"] else "00-00-00"
            fname = f"lines/{n:04d}_{when}_{re.sub(r'[^A-Za-z0-9]+', '', i['name'])}.mp3"
            z.write(i["path"], fname)
            for srt, start in ((srt_real, real[n - 1]), (srt_pack, packed[n - 1])):
                srt.append(f"{n}\n{_srt_time(start)} --> {_srt_time(start + d)}\n{i['name']}: {i['text']}\n")
            manifest["lines"].append(dict(n=n, speaker=i["name"], guid=i["guid"], text=i["text"], ts=i["ts"],
                                          file=fname, duration=round(d, 3),
                                          start_as_it_happened=round(real[n - 1], 3),
                                          start_back_to_back=round(packed[n - 1], 3)))
        z.writestr(f"{base}_as-it-happened.mp3", _mix(clips, real))
        z.writestr(f"{base}_back-to-back.mp3", _mix(clips, packed))
        z.writestr(f"{base}_as-it-happened.srt", "\n".join(srt_real))
        z.writestr(f"{base}_back-to-back.srt", "\n".join(srt_pack))
        z.writestr("manifest.json", json.dumps(manifest, indent=1, ensure_ascii=False))
    os.replace(zpath + ".tmp", zpath)
    g.log(f"voices: exported {len(items)} lines ({len(missing)} missing) -> {os.path.basename(zpath)}")
    return dict(url=f"data/voices/exports/{base}.zip", file=base + ".zip", lines=len(items), missing=len(missing),
                seconds=round(real[-1] + durs[-1], 1))
