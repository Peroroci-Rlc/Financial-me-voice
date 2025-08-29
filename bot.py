import os, re, csv, json, asyncio
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, List

from telegram import Update
from telegram.ext import Application, MessageHandler, CommandHandler, ContextTypes, filters

import speech_recognition as sr
from pydub import AudioSegment

# ---------- Konfigurasi ----------
TZ = os.getenv("TZ", "Asia/Jakarta")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")  # Wajib
USE_SHEETS = os.getenv("USE_SHEETS", "0") == "1"  # Set 1 jika mau pakai Google Sheets
SHEET_ID = os.getenv("SHEET_ID")  # ID Sheet (opsional kalau USE_SHEETS=1)
GSA_JSON = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")  # JSON service account (opsional)

CSV_FILE = "catatan.csv"  # fallback / juga dipakai kalau ga pakai Sheets
FFMPEG_PATH = os.getenv("FFMPEG_PATH", "/usr/bin/ffmpeg")  # di Docker kita install ffmpeg di path ini

# ---------- Optional: Google Sheets ----------
gs = None
ws = None
if USE_SHEETS:
    try:
        import gspread
        from google.oauth2.service_account import Credentials
        creds_dict = json.loads(GSA_JSON)
        scopes = ["https://www.googleapis.com/auth/spreadsheets",
                  "https://www.googleapis.com/auth/drive"]
        creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
        gs = gspread.authorize(creds)
        ws = gs.open_by_key(SHEET_ID).sheet1  # pakai Sheet pertama
    except Exception as e:
        print("Sheets OFF (gagal inisialisasi):", e)
        gs = None
        ws = None

# ---------- Util waktu ----------
def now_jkt():
    # Railway container default UTC; cukup offset manual ke WIB (+7)
    # (Untuk sederhana, kita pakai offset 7 jam)
    return datetime.utcnow() + timedelta(hours=7)

# ---------- Pastikan pydub pakai ffmpeg ----------
AudioSegment.converter = FFMPEG_PATH

# ---------- Transkripsi ----------
async def transcribe_voice_ogg_to_text(ogg_path: str) -> str:
    # Convert OGG/OPUS -> WAV
    wav_path = "temp.wav"
    audio = AudioSegment.from_file(ogg_path, format="ogg")
    audio.export(wav_path, format="wav")

    # SpeechRecognition (gratis, VN pendek)
    recog = sr.Recognizer()
    with sr.AudioFile(wav_path) as source:
        audio_data = recog.record(source)
    # Hasil terbaik bila VN < ~60 detik
    text = recog.recognize_google(audio_data, language="id-ID")
    return text

# ---------- Parsing nominal ----------
DIGIT_RE = re.compile(r'(\d[\d.,]*)\s*(k|rb|ribu|jt|juta)?', re.I)

WORD_NUM = {
    "nol":0,"kosong":0,"satu":1,"se":1,"dua":2,"tiga":3,"empat":4,"lima":5,
    "enam":6,"tujuh":7,"delapan":8,"delapan":8,"delapan":8,"sembilan":9,
    "sepuluh":10,"sebelas":11
}
SCALE = {"puluh":10,"ratus":100,"ribu":1000,"juta":1000000}
SPECIAL_SE = {"seratus":"satu ratus","seribu":"satu ribu","sejuta":"satu juta","sebelas":"satu belas"}

def _normalize_number_words(s: str) -> str:
    s = s.lower().replace("-", " ")
    for k,v in SPECIAL_SE.items():
        s = s.replace(k, v)
    return s

def parse_amount_from_text(text: str) -> Optional[int]:
    """
    1) Coba ambil angka langsung: 25.000 / 25k / 25 rb / 1.2 jt
    2) Kalau tidak ada, coba parse kata: "dua puluh lima ribu"
    """
    # 1) digit first
    m = DIGIT_RE.search(text)
    if m:
        num = m.group(1).replace(".", "").replace(",", "")
        try:
            val = float(num)
        except:
            val = None
        if val is not None:
            mult = m.group(2).lower() if m.group(2) else ""
            if mult in ("k","rb","ribu"):
                val *= 1000
            elif mult in ("jt","juta"):
                val *= 1_000_000
            return int(round(val))

    # 2) words
    s = _normalize_number_words(text)
    tokens = s.split()
    total = 0
    current = 0
    for t in tokens:
        if t in WORD_NUM:
            current += WORD_NUM[t]
        elif t == "belas":
            current += 10
        elif t in SCALE:
            if t in ("ribu","juta"):
                if current == 0:
                    current = 1
                total += current * SCALE[t]
                current = 0
            else:
                # puluh / ratus
                if current == 0:
                    current = 1
                current *= SCALE[t]
        elif t in ("rupiah","rp"):
            continue
        # abaikan kata lain
    total += current
    return total if total > 0 else None

# ---------- Kategori & jenis ----------
PENGELUARAN_KEYS = ["beli","bayar","jajan","makan","minum","ongkir","parkir","pulsa","listrik","token","internet","sewa","tagihan","topup","top up","bbm","gojek","grab","game"]
PEMASUKAN_KEYS =  ["gaji","bonus","refund","masuk","dapet","dapat","jualan","transfer masuk","komisi","tip"]

def infer_jenis(text: str) -> str:
    txt = text.lower()
    if any(k in txt for k in PENGELUARAN_KEYS):
        return "Pengeluaran"
    if any(k in txt for k in PEMASUKAN_KEYS):
        return "Pemasukan"
    # default: pengeluaran kalau ada kata 'beli/bayar/jajan', kalau tidak, asumsi pengeluaran
    return "Pengeluaran"

def infer_kategori(text: str) -> str:
    txt = text.lower()
    rules = [
        ("Makanan & Minuman", ["makan","minum","kopi","teh","nasi","ayam","jajan","snack"]),
        ("Transport", ["bbm","parkir","tol","gojek","grab","bus","kereta","angkot","ojek"]),
        ("Tagihan", ["listrik","token","internet","wifi","pulsa","paket data","pdam","sewa","tagihan"]),
        ("Hiburan & Game", ["game","netflix","spotify","ml","mobile legends","steam"]),
        ("Belanja", ["beli","belanja","shopee","tokopedia","toko"]),
        ("Lainnya", [])
    ]
    for kat, keys in rules:
        if any(k in txt for k in keys):
            return kat
    return "Lainnya"

# ---------- Simpan data ----------
HEADERS = ["Waktu","Jenis","Jumlah","Kategori","Keterangan"]

def ensure_csv():
    if not os.path.exists(CSV_FILE):
        with open(CSV_FILE, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(HEADERS)

def append_record(record: List[Any]):
    # Simpan ke CSV
    ensure_csv()
    with open(CSV_FILE, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(record)
    # Kalau Sheets aktif, push juga
    if ws:
        try:
            ws.append_row(record, value_input_option="USER_ENTERED")
        except Exception as e:
            print("Gagal append ke Sheets:", e)

def load_records() -> List[Dict[str,str]]:
    ensure_csv()
    rows = []
    with open(CSV_FILE, newline="", encoding="utf-8") as f:
        r = csv.DictReader(f)
        for row in r:
            rows.append(row)
    return rows

def summarize(period: str) -> str:
    rows = load_records()
    now = now_jkt()
    total_in = 0
    total_out = 0
    for row in rows:
        try:
            t = datetime.strptime(row["Waktu"], "%Y-%m-%d %H:%M:%S")
        except:
            continue
        if period == "harian":
            if t.date() != now.date(): continue
        elif period == "mingguan":
            if (now.date() - t.date()).days > 7: continue
        elif period == "bulanan":
            if not (t.year == now.year and t.month == now.month): continue

        jumlah = int(row["Jumlah"]) if row["Jumlah"] else 0
        if row["Jenis"] == "Pemasukan":
            total_in += jumlah
        else:
            total_out += jumlah

    saldo = total_in - total_out
    return (f"Ringkasan {period}:\n"
            f"• Pemasukan: Rp{total_in:,}\n"
            f"• Pengeluaran: Rp{total_out:,}\n"
            f"• Saldo: Rp{saldo:,}").replace(",", ".")

# ---------- Telegram Handlers ----------
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = (
        "Halo! 🎙️ Kirim voice note seperti:\n"
        "“Beli kopi dua puluh lima ribu.” atau “Gaji 2 juta.”\n\n"
        "Perintah:\n"
        "• /harian – ringkasan hari ini\n"
        "• /mingguan – ringkasan 7 hari\n"
        "• /bulanan – ringkasan bulan ini\n"
        "• /help – bantuan"
    )
    await update.message.reply_text(msg)

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await start_cmd(update, context)

async def summary_cmd(period: str, update: Update):
    await update.message.reply_text(summarize(period))

async def on_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        file = await update.message.voice.get_file()
        ogg_path = "voice.ogg"
        await file.download_to_drive(ogg_path)

        text = await transcribe_voice_ogg_to_text(ogg_path)
    except Exception as e:
        await update.message.reply_text(f"Gagal transkrip voice: {e}")
        return

    amt = parse_amount_from_text(text)
    jenis = infer_jenis(text)
    kategori = infer_kategori(text)

    if not amt:
        await update.message.reply_text(f"Teks: {text}\n❌ Nominal tidak terdeteksi. Coba sebutkan angka jelas (contoh: “dua puluh ribu” atau “20 ribu”).")
        return

    waktu = now_jkt().strftime("%Y-%m-%d %H:%M:%S")
    append_record([waktu, jenis, str(amt), kategori, text])

    rp = f"Rp{amt:,}".replace(",", ".")
    await update.message.reply_text(
        f"✅ Dicatat!\n"
        f"• {jenis}: {rp}\n"
        f"• Kategori: {kategori}\n"
        f"• Keterangan: {text}"
    )

async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Fallback: kalau kirim teks, tetap diproses
    text = update.message.text
    amt = parse_amount_from_text(text)
    jenis = infer_jenis(text)
    kategori = infer_kategori(text)
    if not amt:
        await update.message.reply_text("Format teks belum jelas. Contoh: 'Beli kopi 25 ribu'.")
        return
    waktu = now_jkt().strftime("%Y-%m-%d %H:%M:%S")
    append_record([waktu, jenis, str(amt), kategori, text])
    rp = f"Rp{amt:,}".replace(",", ".")
    await update.message.reply_text(f"✅ Dicatat! {jenis} {rp} ({kategori})")

def main():
    if not TELEGRAM_TOKEN:
        raise RuntimeError("ENV TELEGRAM_TOKEN belum di-set.")
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("harian", lambda u,c: summary_cmd("harian", u)))
    app.add_handler(CommandHandler("mingguan", lambda u,c: summary_cmd("mingguan", u)))
    app.add_handler(CommandHandler("bulanan", lambda u,c: summary_cmd("bulanan", u)))

    app.add_handler(MessageHandler(filters.VOICE, on_voice))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    print("Bot jalan. Menunggu voice/text...")
    app.run_polling()

if __name__ == "__main__":
    main()
