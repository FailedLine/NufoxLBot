import os
import re
import uuid
import asyncio
import pandas as pd

from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup
)
from telegram.constants import ParseMode
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler,
    CallbackQueryHandler, filters, ContextTypes
)
from telegram.error import BadRequest

# ─── CONFIG ─────────────────────────────────────────────────────────────────────
BOT_TOKEN = os.environ.get("BOT_TOKEN")
TEMP_DIR     = "temp_files"
TEXT_LIMIT   = 335
AUTO_THRESHOLD = 49   # auto-show "Show All" if total > 49
os.makedirs(TEMP_DIR, exist_ok=True)

REQUIRED_CHATS = [
    {"chat_id":"@DxviLZ",        "name":"Announcements", "invite":"https://t.me/DxviLZ"},
    {"chat_id":"-1002343539646", "name":"VIP Lounge",    "invite":"https://t.me/+KKO1tJ1pN_Q3ZjE1"},
    {"chat_id":"-1002495149062", "name":"Dev Chat",      "invite":"https://t.me/+eusIzYrovnRlZGQ1"},
]

TRANSFORMS = [
    ("Original",        lambda nums: nums),
    ("Added '+'",       lambda nums: ["+"+n if not n.startswith("+") else n for n in nums]),
    ("Removed '+'",     lambda nums: [n[1:] if n.startswith("+") else n for n in nums]),
    ("No country code", lambda nums: [re.sub(r"^\+?\d{1,3}", "", n) for n in nums]),
    ("+ No cc",         lambda nums: ["+"+re.sub(r"^\+?\d{1,3}", "", n) for n in nums]),
]

user_data: dict[int, dict] = {}

# ─── UTILITIES ─────────────────────────────────────────────────────────────────
async def safe_edit(q, text, **kwargs):
    try:
        await q.edit_message_text(text, **kwargs)
    except BadRequest as e:
        # ignore "message not modified" errors
        if "not modified" in str(e).lower():
            return
        raise

async def get_membership(ctx, uid:int):
    statuses = []
    for cfg in REQUIRED_CHATS:
        try:
            m = await ctx.bot.get_chat_member(cfg["chat_id"], uid)
            statuses.append(m.status in ("creator","administrator","member"))
        except:
            statuses.append(False)
    return statuses

def extract_numbers(txt:str):
    return re.findall(r"\+?\d{7,15}", txt)

def dedupe(nums:list[str]):
    seen, out = set(), []
    for n in nums:
        n = n.strip()
        if n and n not in seen:
            seen.add(n)
            out.append(n)
    return out

def parse_file(path:str, ext:str):
    try:
        if ext==".txt":
            raw = open(path,encoding="utf-8").read()
        elif ext==".csv":
            df = pd.read_csv(path, dtype=str)
            raw = " ".join(df.stack().fillna("").astype(str))
        else:
            df = pd.read_excel(path, dtype=str)
            raw = " ".join(df.stack().fillna("").astype(str))
        return extract_numbers(raw)
    except:
        return None

# ─── MENUS ──────────────────────────────────────────────────────────────────────
def build_join_menu(miss:list[int]):
    kb, lines = [], []
    for i in miss:
        lines.append(f"❌ {REQUIRED_CHATS[i]['name']}")
        kb.append([InlineKeyboardButton(
            f"Join {REQUIRED_CHATS[i]['name']}", url=REQUIRED_CHATS[i]['invite']
        )])
    text = (
        "🔒 *Access Restricted*\n\n"
        "Please join these channels:\n"
        + "\n".join(lines)
        + "\n\n_When done, tap_ ✅ *I’ve Joined – Check*"
    )
    kb.append([InlineKeyboardButton("✅ I’ve Joined – Check", callback_data="check_joins")])
    return text, InlineKeyboardMarkup(kb)

def build_main_menu(uid:int):
    cfg  = user_data[uid]
    nums = cfg["history"][cfg["current_state"]]

    # If no numbers yet, show welcome prompt
    if not nums:
        return (
            "> 📥 *Welcome!*\n\n"
            "Send phone numbers as text or upload a file (txt/csv/xlsx).",
            None
        )

    total = len(nums)
    plus  = sum(n.startswith("+") for n in nums)
    minus = total - plus
    header = f"🧮 Total: {total}   |   ✅ With +: {plus}   |   ❌ Without +: {minus}"

    kb = [
      # Row1: Export | Add+/Remove+
      [
        InlineKeyboardButton("📤 Export", callback_data="export"),
        InlineKeyboardButton(
            "➖ Remove +" if cfg["current_state"]==1 else "➕ Add +",
            callback_data="transform"
        ),
      ],
      # Row2: Show Num | Settings
      [
        InlineKeyboardButton("📄 Show num", callback_data="view_0"),
        InlineKeyboardButton("⚙️ Settings", callback_data="settings"),
      ],
      # Row3: New Season
      [ InlineKeyboardButton("☑️ INPUT / SEND FILE", callback_data="new_season") ]
    ]
    return header, InlineKeyboardMarkup(kb)

def build_export_menu():
    # one line, uppercase
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("TXT", callback_data="export_txt"),
        InlineKeyboardButton("CSV", callback_data="export_csv"),
        InlineKeyboardButton("XLSX",callback_data="export_xlsx"),
        InlineKeyboardButton("⬅ Back",callback_data="back_to_menu")
    ]])

def build_settings_menu(uid:int):
    cfg = user_data[uid]
    kb = InlineKeyboardMarkup([
        [ InlineKeyboardButton(
            f"Show All: {'On' if cfg['show_all_enabled'] else 'Off'}",
            callback_data="toggle_show_all"
        )],
        [ InlineKeyboardButton(
            f"Page size: {cfg['per_page']}",
            callback_data="set_per_page"
        )],
        [ InlineKeyboardButton("⬅ Back", callback_data="back_to_menu") ]
    ])
    return "⚙️ *Settings*\n\nToggle or set values:", kb

async def send_page(
    uid:int, who, ctx:ContextTypes.DEFAULT_TYPE,
    offset:int, show_all:bool=False
):
    cfg   = user_data[uid]
    nums  = cfg["history"][cfg["current_state"]]
    per   = cfg["per_page"]
    total = len(nums)
    lim   = total if show_all else per
    start = offset+1
    end   = min(offset+lim, total)
    pages = (total-1)//per + 1
    page  = offset//per + 1

    header = (
        f"📄 Viewing {start}–{end} of {total}\n"
        f"📍 Page {page} of {pages}\n\n"
    )
    body = "\n".join(nums[offset:offset+lim])

    nav = []
    step = per
    if offset>0:
        nav.append(InlineKeyboardButton(
            "⬅ Prev",
            callback_data=f"{'viewall' if show_all else 'view'}_{max(0,offset-step)}"
        ))
    if offset+lim<total:
        nav.append(InlineKeyboardButton(
            "Next ➡",
            callback_data=f"{'viewall' if show_all else 'view'}_{offset+step}"
        ))

    rows = [nav] if nav else []
    # only show Show All if manually ON or total > threshold
    if not show_all and (cfg["show_all_enabled"] or total> AUTO_THRESHOLD):
        rows.append([InlineKeyboardButton("Show All", callback_data="viewall_0")])
    rows.append([InlineKeyboardButton("⬅ Back", callback_data="back_to_menu")])

    kb   = InlineKeyboardMarkup(rows)
    text = header + body

    if isinstance(who, Update):
        await who.message.reply_text(text, reply_markup=kb, parse_mode=ParseMode.MARKDOWN)
    else:
        await safe_edit(who, text, reply_markup=kb, parse_mode=ParseMode.MARKDOWN)

# ─── HANDLERS ───────────────────────────────────────────────────────────────────

async def start(update:Update, ctx:ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    user_data[uid] = {
        "session_id":      str(uuid.uuid4()),
        "history":         [[]],
        "current_state":   0,
        "per_page":        10,
        "show_all_enabled": False
    }
    joined = await get_membership(ctx, uid)
    if not all(joined):
        miss = [i for i,ok in enumerate(joined) if not ok]
        text,kb = build_join_menu(miss)
        return await update.message.reply_text(text, reply_markup=kb, parse_mode=ParseMode.MARKDOWN)

    text,kb = build_main_menu(uid)
    return await update.message.reply_text(text, reply_markup=kb, parse_mode=ParseMode.MARKDOWN)

async def handle_text(update:Update, ctx:ContextTypes.DEFAULT_TYPE):
    uid=update.effective_user.id
    if uid not in user_data:
        return await update.message.reply_text(
            "⚠️ *Session expired.* Use /start", parse_mode=ParseMode.MARKDOWN
        )
    cfg=user_data[uid]

    nums=dedupe(extract_numbers(update.message.text or ""))
    if not nums:
        return await update.message.reply_text("⚠️ *No numbers found.*", parse_mode=ParseMode.MARKDOWN)

    joined=await get_membership(ctx,uid)
    if not all(joined):
        cfg["pending"]=nums
        miss=[i for i,ok in enumerate(joined) if not ok]
        text,kb=build_join_menu(miss)
        return await update.message.reply_text(text,reply_markup=kb,parse_mode=ParseMode.MARKDOWN)

    if len(nums)>=TEXT_LIMIT:
        warn=(
          "⚠️ *Large Dataset Detected!*\n\n"
          f"> *You’ve sent {len(nums)} numbers, exceeding {TEXT_LIMIT}.*\n\n"
          "📝 *Use a file for large sets.*\n⚡ *Processing anyway…*"
        )
        await update.message.reply_text(warn, parse_mode=ParseMode.MARKDOWN)
        await asyncio.sleep(0.5)

    cfg["history"]=[nums]
    cfg["current_state"]=0

    text,kb=build_main_menu(uid)
    return await update.message.reply_text(text,reply_markup=kb,parse_mode=ParseMode.MARKDOWN)

async def handle_file(update:Update, ctx:ContextTypes.DEFAULT_TYPE):
    uid=update.effective_user.id
    if uid not in user_data:
        return await update.message.reply_text(
            "⚠️ *Session expired.* Use /start", parse_mode=ParseMode.MARKDOWN
        )
    cfg=user_data[uid]

    doc=update.message.document
    ext=os.path.splitext(doc.file_name)[1].lower()
    if ext not in (".txt",".csv",".xls",".xlsx"):
        return await update.message.reply_text("⚠️ *Unsupported file type.*", parse_mode=ParseMode.MARKDOWN)

    path=os.path.join(TEMP_DIR,f"{uid}_{uuid.uuid4().hex}{ext}")
    f=await doc.get_file(); await f.download_to_drive(path)
    nums=parse_file(path,ext) or []
    os.remove(path)

    if not nums:
        return await update.message.reply_text("⚠️ *No numbers in file.*", parse_mode=ParseMode.MARKDOWN)

    joined=await get_membership(ctx,uid)
    if not all(joined):
        cfg["pending"]=nums
        miss=[i for i,ok in enumerate(joined) if not ok]
        text,kb=build_join_menu(miss)
        return await update.message.reply_text(text,reply_markup=kb,parse_mode=ParseMode.MARKDOWN)

    cfg["history"]=[dedupe(nums)]
    cfg["current_state"]=0

    text,kb=build_main_menu(uid)
    return await update.message.reply_text(f"✅ *File processed!*\n\n{text}",
                                          reply_markup=kb,parse_mode=ParseMode.MARKDOWN)

async def handle_callback(update:Update, ctx:ContextTypes.DEFAULT_TYPE):
    q=update.callback_query; data=q.data; uid=q.from_user.id
    await q.answer()

    if uid not in user_data:
        return await safe_edit(q,"⚠️ *Session expired.* Use /start",parse_mode=ParseMode.MARKDOWN)
    cfg=user_data[uid]

    # Re-check joins
    if data=="check_joins":
        joined=await get_membership(ctx,uid)
        if not all(joined):
            miss=[i for i,ok in enumerate(joined) if not ok]
            text,kb=build_join_menu(miss)
            return await safe_edit(q,text,reply_markup=kb,parse_mode=ParseMode.MARKDOWN)
        pend=cfg.pop("pending",None)
        if pend:
            cfg["history"]=[pend]; cfg["current_state"]=0
        text,kb=build_main_menu(uid)
        return await safe_edit(q,text,reply_markup=kb,parse_mode=ParseMode.MARKDOWN)

    # New Season
    if data=="new_season":
        cfg["session_id"]=str(uuid.uuid4())
        prompt=(
          "✅ *New session started.*\n"
          "Send new numbers or upload a file.\n"
          "_You can still view the old numbers until new input is provided._"
        )
        _,kb=build_main_menu(uid)
        return await safe_edit(q,prompt,reply_markup=kb,parse_mode=ParseMode.MARKDOWN)

    # Export
    if data=="export":
        return await safe_edit(q,"📤 *CHOOSE FORMAT:*",
                               reply_markup=build_export_menu(),parse_mode=ParseMode.MARKDOWN)
    if data.startswith("export_"):
        ext=data.split("_",1)[1]
        nums=cfg["history"][cfg["current_state"]]
        if not nums:
            return await safe_edit(q,"⚠️ *No numbers yet.*",parse_mode=ParseMode.MARKDOWN)
        fname=f"numbers_{uid}.{ext}"
        path=os.path.join(TEMP_DIR,fname)
        if ext=="txt":
            open(path,"w",encoding="utf-8").write("\n".join(nums))
        elif ext=="csv":
            pd.DataFrame(nums,columns=["Phone Numbers"]).to_csv(path,index=False)
        else:
            pd.DataFrame(nums,columns=["Phone Numbers"]).to_excel(path,index=False)
        await q.message.reply_document(open(path,"rb"),filename=fname)
        os.remove(path)
        text,kb=build_main_menu(uid)
        return await safe_edit(q,f"{text}\n✅ *EXPORT COMPLETE!*",
                               reply_markup=kb,parse_mode=ParseMode.MARKDOWN)

    # Paging / Show All
    if data.startswith("view"):
        off=int(data.split("_",1)[1])
        if not cfg["history"][cfg["current_state"]]:
            return await safe_edit(q,"⚠️ *No numbers yet.*",parse_mode=ParseMode.MARKDOWN)
        return await send_page(uid,q,ctx,off,show_all=False)
    if data.startswith("viewall"):
        off=int(data.split("_",1)[1])
        return await send_page(uid,q,ctx,off,show_all=True)

    # Transform
    if data=="transform":
        base=cfg["history"][0]
        if not base:
            return await safe_edit(q,"⚠️ *No numbers yet.*",parse_mode=ParseMode.MARKDOWN)
        idx=(cfg["current_state"]+1)%len(TRANSFORMS)
        _,fn=TRANSFORMS[idx]
        new=fn(base)
        if len(cfg["history"])<len(TRANSFORMS):
            cfg["history"].append(new)
        else:
            cfg["history"][idx]=new
        cfg["current_state"]=idx
        text,kb=build_main_menu(uid)
        return await safe_edit(q,f"{text}\n✅ *Applied:* {TRANSFORMS[idx][0]}",
                               reply_markup=kb,parse_mode=ParseMode.MARKDOWN)

    # Settings
    if data=="settings":
        text,kb=build_settings_menu(uid)
        return await safe_edit(q,text,reply_markup=kb,parse_mode=ParseMode.MARKDOWN)

    if data=="toggle_show_all":
        cfg["show_all_enabled"]=not cfg["show_all_enabled"]
        text,kb=build_settings_menu(uid)
        return await safe_edit(q,text,reply_markup=kb,parse_mode=ParseMode.MARKDOWN)

    if data=="set_per_page":
        cfg["waiting_for"]="per_page"
        return await safe_edit(q,
          f"🔢 *SEND NEW PAGE SIZE* (current {cfg['per_page']}).",
          parse_mode=ParseMode.MARKDOWN
        )

    if data=="back_to_menu":
        text,kb=build_main_menu(uid)
        return await safe_edit(q,text,reply_markup=kb,parse_mode=ParseMode.MARKDOWN)

async def handle_settings_input(update:Update, ctx:ContextTypes.DEFAULT_TYPE):
    uid=update.effective_user.id
    if uid not in user_data:
        return await update.message.reply_text("⚠️ *Session expired.* Use /start",parse_mode=ParseMode.MARKDOWN)
    cfg=user_data[uid]
    mode=cfg.get("waiting_for")
    if not mode:
        return await handle_text(update,ctx)

    try:
        val=int(update.message.text.strip())
        if val<=0: raise ValueError
    except:
        return await update.message.reply_text("⚠️ *Please send a positive integer.*",parse_mode=ParseMode.MARKDOWN)

    if mode=="per_page":
        cfg["per_page"]=val
    cfg.pop("waiting_for",None)

    text,kb=build_settings_menu(uid)
    return await update.message.reply_text(text,reply_markup=kb,parse_mode=ParseMode.MARKDOWN)

def main():
    app=ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start",start))
    app.add_handler(MessageHandler(filters.Document.ALL,handle_file))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND,handle_settings_input))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.ALL,handle_text))
    app.run_polling()

if __name__=="__main__":
    main()
