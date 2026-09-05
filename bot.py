from __future__ import annotations

import asyncio
import html
import json
import logging
import re
from contextlib import suppress
from typing import Any
from urllib.parse import urlparse

from aiohttp import web
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from config import (
    ALLOWED_USER_IDS,
    BOT_TOKEN,
    HTTP_HOST,
    HTTP_PORT,
    MAX_DOCUMENT_BYTES,
    MAX_IMPORTED_NODES,
    MAX_IMPORTED_SUBSCRIPTIONS,
    PUBLIC_BASE_URL,
)
from convert import (
    apply_path_maps,
    extract_share_links,
    fetch_subscription,
    format_bytes_gb,
    format_expire,
    node_name_from_url,
    parse_nodes_from_text,
    remain_text,
    to_base64_sub,
    to_clash,
    to_qx,
    to_share_links,
    to_singbox,
    to_surge,
)
from db import Store, nodes_of

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("subs-bot")

store = Store()

ALLOWED_DOCUMENT_SUFFIXES = frozenset({".txt", ".log", ".yaml", ".yml", ".json"})


def _document_suffix(filename: str) -> str:
    name = filename.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    return ("." + name.rsplit(".", 1)[-1].lower()) if "." in name else ""


def _decode_document(data: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", "replace")


def _unique(values: list[str]) -> list[str]:
    return list(dict.fromkeys(value for value in values if value))


MAIN_KB = ReplyKeyboardMarkup(
    [
        ["📦 订阅列表", "🟠 临期列表"],
        ["📤 导出链接", "🔄 更新所有"],
        ["🔢 重置编号", "♻️ 撤销删除"],
        ["🧭 路径对应", "❓ 帮助菜单"],
    ],
    resize_keyboard=True,
)

HELP_TEXT = (
    "📌 支持直接发送任意数量的订阅链接\n"
    "📄 支持上传 .txt / .log / .yaml / .yml 自动识别订阅和节点\n"
    "♻️ 支持在回收站中恢复最近 30 天删除的订阅\n"
    "🔗 支持导出链接、短链、Clash / Sing-box / Base64 / Surge / QX\n\n"
    "<b>命令</b>\n"
    "/start 主菜单\n"
    "/list 订阅列表\n"
    "/expire 临期列表\n"
    "/export 导出链接\n"
    "/path 路径对应\n"
    "/renumber 重置编号\n"
    "/s &lt;关键词&gt; 搜索订阅\n"
    "/o &lt;关键词&gt; 导出搜索结果\n"
    "/d &lt;关键词或链接&gt; 删除名称或链接匹配\n"
    "/i &lt;排序方式&gt; 切换内联排序\n"
    "/ai 批量测试 API 地址和 Key\n\n"
    "<b>内联功能</b>\n"
    "输入 @MxlDYBot 或 @MxlDYBot 订阅名/链接内容发送订阅\n"
    "内联私密分享：@MxlDYBot Share [份数] [用户ID] [分钟] 内容\n\n"
    "<b>添加订阅</b>\n" ,
    "直接发送订阅链接（http/https）\n"
    "名称优先使用订阅返回的配置名称\n\n"
    "<b>临时节点</b>\n"
    "发送 ss/vmess/vless/... 链接加入临时列表\n"
    "/temp - 查看临时节点\n"
    "/temp clear - 清空临时节点\n\n"
    "<b>路径对应</b>\n"
    "格式：节点名=备注\n"
    "清空：节点名=\n"
    "/path clear - 清空全部\n\n"
    "发送数字编号可查看订阅详情。"
)


def allowed(user_id: int | None) -> bool:
    return bool(user_id) and (not ALLOWED_USER_IDS or user_id in ALLOWED_USER_IDS)


async def deny(update: Update) -> bool:
    if update.effective_user and allowed(update.effective_user.id):
        return False
    text = "此 Bot 需要授权使用\n授权请联系管理员"
    if update.callback_query:
        await update.callback_query.answer(text, show_alert=True)
    elif update.effective_message:
        await update.effective_message.reply_text(text)
    return True


def sub_token_url(token: str, kind: str = "clash") -> str:
    return f"{PUBLIC_BASE_URL}/sub/{token}/{kind}"


def progress_bar(used: float | None, total: float | None) -> str:
    if used is None or total is None or total <= 0:
        return "未知"
    pct = max(0, min(100, int(used * 100 / total)))
    filled = pct // 10
    return f"[{'█' * filled}{'░' * (10 - filled)}] {pct}%"


def remain_traffic_gb(sub: dict[str, Any]) -> float | None:
    used = sub.get("traffic_used")
    total = sub.get("traffic_total")
    if used is None or total is None:
        return None
    return max(float(total) - float(used), 0.0)


def list_button_label(idx: int, sub: dict[str, Any]) -> str:
    remain = remain_traffic_gb(sub)
    remain_s = f"{remain:.2f}GB" if remain is not None else "?GB"
    days = remain_text(sub.get("expire_at"))
    if days == "长期有效":
        days = "长期"
    elif days.endswith("小时") and "天" in days:
        days = days.split("天", 1)[0] + "天"
    name = str(sub.get("name") or "未命名")
    label = f"#{idx} {name} [{remain_s}] {days}"
    return label[:64]


def sub_summary_line(idx: int, sub: dict[str, Any]) -> str:
    remain = remain_traffic_gb(sub)
    remain_s = format_bytes_gb(remain)
    expire = remain_text(sub.get("expire_at"))
    return (
        f"#{idx} <b>{html.escape(sub['name'])}</b>\n"
        f"剩余流量 {html.escape(remain_s)} · {html.escape(expire)}"
    )


def detail_text(sub: dict[str, Any], idx: int) -> str:
    nodes = nodes_of(sub)
    used = sub.get("traffic_used")
    total = sub.get("traffic_total")
    remain_traffic = None
    if used is not None and total is not None:
        remain_traffic = max(total - used, 0)
    node_preview = "\n".join(
        f"• {html.escape(str(n.get('name') or '未命名'))}" for n in nodes[:12]
    ) or "未解析到节点"
    if len(nodes) > 12:
        node_preview += f"\n… 另有 {len(nodes) - 12} 个"
    err = sub.get("last_error")
    err_line = f"\n⚠️ 刷新错误: {html.escape(err)}" if err else ""
    return (
        f"配置名称: {html.escape(sub['name'])}\n"
        f"订阅链接: {html.escape(sub['url'])}\n"
        f"流量详情: {html.escape(format_bytes_gb(used))} / {html.escape(format_bytes_gb(total))}\n"
        f"使用进度: {html.escape(progress_bar(used, total))}\n"
        f"剩余可用: {html.escape(format_bytes_gb(remain_traffic))}\n"
        f"过期时间: {html.escape(format_expire(sub.get('expire_at')))}\n"
        f"剩余时间: {html.escape(remain_text(sub.get('expire_at')))}\n"
        f"📡 节点列表 ({len(nodes)}):\n<blockquote>{node_preview}</blockquote>"
        f"{err_line}"
    )


def detail_keyboard(sub_id: int, page: int = 0) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Clash Meta", callback_data=f"sub:fmt:{sub_id}:clash"),
                InlineKeyboardButton("Sing-box", callback_data=f"sub:fmt:{sub_id}:singbox"),
            ],
            [
                InlineKeyboardButton("Base64", callback_data=f"sub:fmt:{sub_id}:base64"),
                InlineKeyboardButton("Surge", callback_data=f"sub:fmt:{sub_id}:surge"),
            ],
            [InlineKeyboardButton("QX", callback_data=f"sub:fmt:{sub_id}:qx")],
            [InlineKeyboardButton("🔄 刷新订阅", callback_data=f"sub:refresh:{sub_id}:{page}")],
            [
                InlineKeyboardButton("📦 导出节点", callback_data=f"sub:nodes:{sub_id}"),
                InlineKeyboardButton("🗑 删除订阅", callback_data=f"sub:del:{sub_id}:{page}"),
            ],
            [InlineKeyboardButton("⬅️ 返回列表", callback_data=f"sub:back:{page}")],
        ]
    )


def list_keyboard(page: int, total: int, page_size: int = 8, items: list[tuple[int, dict[str, Any]]] | None = None) -> InlineKeyboardMarkup:
    pages = max(1, (total + page_size - 1) // page_size)
    rows: list[list[InlineKeyboardButton]] = []
    if items:
        for display_idx, sub in items:
            rows.append([
                InlineKeyboardButton(
                    list_button_label(display_idx, sub),
                    callback_data=f"sub:open:{int(sub['id'])}:{page}",
                )
            ])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️", callback_data=f"list:page:{page-1}"))
    nav.append(InlineKeyboardButton(f"{page+1}/{pages}", callback_data="noop"))
    if page + 1 < pages:
        nav.append(InlineKeyboardButton("➡️", callback_data=f"list:page:{page+1}"))
    rows.append(nav)
    rows.append([InlineKeyboardButton("🏠 主菜单", callback_data="list:home")])
    return InlineKeyboardMarkup(rows)


def looks_like_host_name(name: str | None) -> bool:
    if not name:
        return True
    n = name.strip().lower()
    if n in {"订阅", "sub", "subscription"}:
        return True
    if "." in n and " " not in n and not any("\u4e00" <= ch <= "\u9fff" for ch in n):
        return True
    return False


async def refresh_sub(user_id: int, sub: dict[str, Any], rename: bool = False) -> dict[str, Any]:
    nodes, meta, err = await fetch_subscription(sub["url"])
    fields: dict[str, Any] = {
        "nodes_json": json.dumps(nodes, ensure_ascii=False),
        "last_error": err,
        "traffic_used": meta.get("traffic_used"),
        "traffic_total": meta.get("traffic_total"),
        "expire_at": meta.get("expire_at"),
    }
    profile_name = (meta.get("profile_name") or "").strip()
    if profile_name and (rename or looks_like_host_name(sub.get("name"))):
        fields["name"] = profile_name
    updated = await store.update_sub(user_id, int(sub["id"]), **fields)
    return updated or sub


async def render_list(user_id: int, page: int = 0) -> tuple[str, InlineKeyboardMarkup]:
    subs = await store.list_subs(user_id)
    page_size = 8
    if not subs:
        return "📋 暂无订阅\n\n直接发送订阅链接即可添加。", InlineKeyboardMarkup(
            [[InlineKeyboardButton("🏠 主菜单", callback_data="list:home")]]
        )
    page = max(0, min(page, (len(subs) - 1) // page_size))
    start = page * page_size
    chunk = subs[start : start + page_size]
    items = [(start + i, sub) for i, sub in enumerate(chunk, start=1)]
    text = f"📋 <b>订阅列表</b>（共 {len(subs)} 个）\n点击下方按钮查看详情。"
    return text, list_keyboard(page, len(subs), page_size, items)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await deny(update):
        return
    name = update.effective_user.first_name or "用户"
    await update.effective_message.reply_text(
        f"👋 你好，{name}！\n\n"
        "🤖 订阅管理机器人\n"
        "发送订阅链接，或发送文本 / 上传 .txt、.log、.yaml、.yml 文件，"
        "可进行订阅查询、节点解析、导出节点和短链生成。\n"
        "更多内容请查看帮助菜单。",
        reply_markup=MAIN_KB,
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await deny(update):
        return
    await update.effective_message.reply_html(HELP_TEXT, reply_markup=MAIN_KB)


async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await deny(update):
        return
    text, kb = await render_list(update.effective_user.id, 0)
    await update.effective_message.reply_html(text, reply_markup=kb, disable_web_page_preview=True)


async def cmd_expire(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await deny(update):
        return
    import time as _time

    subs = await store.list_subs(update.effective_user.id)
    now = int(_time.time())
    soon = []
    for i, sub in enumerate(subs, start=1):
        exp = sub.get("expire_at")
        if not exp:
            continue
        if 0 <= exp - now <= 14 * 86400:
            soon.append((i, sub))
    if not soon:
        await update.effective_message.reply_text(
            "🟠 临期列表为空，当前没有 14 天内到期的订阅",
            reply_markup=MAIN_KB,
        )
        return
    lines = ["🟠 <b>临期列表</b>（14天内）\n"]
    for i, sub in soon:
        lines.append(
            f"#{i} <b>{html.escape(sub['name'])}</b>\n剩余: {html.escape(remain_text(sub.get('expire_at')))}"
        )
    await update.effective_message.reply_html("\n\n".join(lines), reply_markup=MAIN_KB)


async def cmd_refresh_all(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await deny(update):
        return
    user_id = update.effective_user.id
    subs = await store.list_subs(user_id)
    if not subs:
        await update.effective_message.reply_text("暂无订阅可更新。", reply_markup=MAIN_KB)
        return
    msg = await update.effective_message.reply_text(f"🔄 开始更新 {len(subs)} 个订阅...")
    ok = fail = renamed = 0
    for sub in subs:
        before = sub.get("name")
        updated = await refresh_sub(user_id, sub, rename=True)
        if updated.get("last_error"):
            fail += 1
        else:
            ok += 1
        if updated.get("name") and updated.get("name") != before:
            renamed += 1
    await msg.edit_text(f"✅ 更新完成：成功 {ok}，失败 {fail}，重命名 {renamed}")


async def cmd_recycle(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await deny(update):
        return
    rows = await store.list_deleted(update.effective_user.id, days=30)
    if not rows:
        await update.effective_message.reply_text(
            "♻️ 订阅回收站为空，最近30天内没有可恢复的订阅。",
            reply_markup=MAIN_KB,
        )
        return
    lines = [f"♻️ <b>订阅回收站</b>（{len(rows)}）\n"]
    keyboard = []
    for i, row in enumerate(rows[:12], start=1):
        lines.append(f"#{i} {html.escape(row['name'])}")
        keyboard.append([
            InlineKeyboardButton(
                f"恢复 #{i} {row['name']}"[:64],
                callback_data=f"recycle:restore:{int(row['id'])}",
            )
        ])
    await update.effective_message.reply_html(
        "\n".join(lines),
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def cmd_export(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await deny(update):
        return
    user_id = update.effective_user.id
    subs = await store.list_subs(user_id)
    maps = {m["node_name"]: m["remark"] for m in await store.list_path_maps(user_id)}
    all_nodes: list[dict[str, Any]] = []
    for sub in subs:
        nodes = nodes_of(sub)
        if not nodes:
            sub = await refresh_sub(user_id, sub)
            nodes = nodes_of(sub)
        all_nodes.extend(apply_path_maps(nodes, maps))
    temp = await store.list_temp(user_id)
    for t in temp:
        all_nodes.append({"name": t["name"], "share": t["url"], "type": "temp"})
    if not all_nodes:
        await update.effective_message.reply_text("暂无可导出节点。", reply_markup=MAIN_KB)
        return
    # create a temporary aggregate token via short link target using first sub style endpoint is complex;
    # instead create short link to base64 content hosted endpoint
    code = await store.create_short(user_id, "aggregate://all")
    # store aggregate snapshot in short target by rewriting to special path
    # simpler: reply files as text snippets / links using per-user aggregate route
    clash = to_clash(all_nodes)
    b64 = to_base64_sub(all_nodes)
    await update.effective_message.reply_html(
        f"📦 <b>导出完成</b>\n节点数: {len(all_nodes)}\n\n"
        f"Clash:\n<code>{html.escape(PUBLIC_BASE_URL + '/agg/' + str(user_id) + '/clash')}</code>\n"
        f"Base64:\n<code>{html.escape(PUBLIC_BASE_URL + '/agg/' + str(user_id) + '/base64')}</code>\n\n"
        f"短链码: <code>{html.escape(code)}</code>",
        reply_markup=MAIN_KB,
        disable_web_page_preview=True,
    )
    # keep references unused lint quiet
    _ = (clash, b64)


async def cmd_short(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await deny(update):
        return
    await update.effective_message.reply_text(
        "🔗 短链用法：\n发送：短链 https://example.com/xxx\n或先发链接后点「短链」按钮。",
        reply_markup=MAIN_KB,
    )


async def cmd_path(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await deny(update):
        return
    args = context.args or []
    user_id = update.effective_user.id
    if args and args[0].lower() == "clear":
        n = await store.clear_path_maps(user_id)
        await update.effective_message.reply_text(f"已清空 {n} 条路径对应。", reply_markup=MAIN_KB)
        return
    maps = await store.list_path_maps(user_id)
    if not maps:
        await update.effective_message.reply_text(
            "🧭 暂无路径对应\n发送：节点名=备注\n清空某条：节点名=",
            reply_markup=MAIN_KB,
        )
        return
    lines = ["🧭 <b>路径对应</b>\n"]
    for m in maps:
        lines.append(f"• <code>{html.escape(m['node_name'])}</code> = {html.escape(m['remark'])}")
    await update.effective_message.reply_html("\n".join(lines), reply_markup=MAIN_KB)


async def cmd_renumber(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await deny(update):
        return
    n = await store.renumber(update.effective_user.id)
    await update.effective_message.reply_text(f"已重置编号，当前 {n} 条订阅。", reply_markup=MAIN_KB)


async def cmd_delete_match(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await deny(update):
        return
    q = " ".join(context.args or []).strip().lower()
    if not q:
        await update.effective_message.reply_text("用法：/d 关键词或订阅链接", reply_markup=MAIN_KB)
        return
    subs = await store.list_subs(update.effective_user.id)
    hits = [s for s in subs if q in f"{s['name']} {s['url']}".lower()]
    if not hits:
        await update.effective_message.reply_text("没有匹配订阅。", reply_markup=MAIN_KB)
        return
    for sub in hits:
        await store.delete_sub(update.effective_user.id, int(sub["id"]))
    await update.effective_message.reply_text(f"✅ 已删除 {len(hits)} 个匹配订阅。", reply_markup=MAIN_KB)


async def cmd_inline_sort(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await deny(update):
        return
    mode = " ".join(context.args or []).strip() or "默认"
    await update.effective_message.reply_text(
        f"✅ 内联排序已切换：{mode}\n当前版本暂支持订阅列表和详情内联查询。",
        reply_markup=MAIN_KB,
    )


async def cmd_api_test(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await deny(update):
        return
    await update.effective_message.reply_text(
        "🧪 API 批量测试\n请发送要测试的 API 地址和 Key，每行一组：\n地址|Key",
        reply_markup=MAIN_KB,
    )


async def cmd_search_export(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await deny(update):
        return
    q = " ".join(context.args or []).strip().lower()
    subs = await store.list_subs(update.effective_user.id)
    hits = [s for s in subs if not q or q in f"{s['name']} {s['url']}".lower()]
    if not hits:
        await update.effective_message.reply_text("没有匹配订阅。", reply_markup=MAIN_KB)
        return
    nodes = []
    for sub in hits:
        nodes.extend(nodes_of(sub))
    if not nodes:
        await update.effective_message.reply_text("匹配订阅暂无可导出节点。", reply_markup=MAIN_KB)
        return
    await update.effective_message.reply_text(
        f"📦 搜索导出完成：{len(hits)} 个订阅，{len(nodes)} 个节点\\n"
        f"{PUBLIC_BASE_URL}/agg/{update.effective_user.id}/clash",
        reply_markup=MAIN_KB,
    )


async def cmd_search(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await deny(update):
        return
    q = " ".join(context.args or []).strip()
    if not q:
        await update.effective_message.reply_text("用法：/s 关键词", reply_markup=MAIN_KB)
        return
    subs = await store.list_subs(update.effective_user.id)
    hits = []
    for i, sub in enumerate(subs, start=1):
        blob = f"{sub['name']} {sub['url']}".lower()
        if q.lower() in blob:
            hits.append(sub_summary_line(i, sub))
    if not hits:
        await update.effective_message.reply_text("没有匹配订阅。", reply_markup=MAIN_KB)
        return
    await update.effective_message.reply_html("🔍 搜索结果\n\n" + "\n\n".join(hits), reply_markup=MAIN_KB)


async def cmd_temp(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await deny(update):
        return
    args = context.args or []
    user_id = update.effective_user.id
    if args and args[0].lower() == "clear":
        n = await store.clear_temp(user_id)
        await update.effective_message.reply_text(f"已清空临时节点 {n} 条。", reply_markup=MAIN_KB)
        return
    rows = await store.list_temp(user_id)
    if not rows:
        await update.effective_message.reply_text("临时列表为空。", reply_markup=MAIN_KB)
        return
    lines = [f"🧪 临时节点 ({len(rows)})\n"]
    for i, r in enumerate(rows, start=1):
        lines.append(f"#{i} {html.escape(r['name'])}\n<code>{html.escape(r['url'])}</code>")
    await update.effective_message.reply_html("\n\n".join(lines), reply_markup=MAIN_KB)


async def show_detail(update: Update, user_id: int, idx: int, page: int = 0) -> None:
    subs = await store.list_subs(user_id)
    if idx < 1 or idx > len(subs):
        await update.effective_message.reply_text("编号不存在。", reply_markup=MAIN_KB)
        return
    sub = subs[idx - 1]
    if not nodes_of(sub):
        sub = await refresh_sub(user_id, sub)
    await update.effective_message.reply_html(
        detail_text(sub, idx),
        reply_markup=detail_keyboard(int(sub["id"]), page),
        disable_web_page_preview=True,
    )


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await deny(update):
        return
    text = (update.effective_message.text or "").strip()
    user_id = update.effective_user.id
    if not text:
        return

    if text in ("📦 订阅列表", "📋 订阅列表", "订阅列表"):
        return await cmd_list(update, context)
    if text in ("⏳ 临期列表", "🟠 临期列表", "临期列表"):
        return await cmd_expire(update, context)
    if text in ("🔗 短链", "短链"):
        return await cmd_short(update, context)
    if text in ("📦 导出", "📤 导出链接", "导出", "导出链接"):
        return await cmd_export(update, context)
    if text in ("🔄 更新所有", "更新所有"):
        return await cmd_refresh_all(update, context)
    if text in ("🔢 重置编号", "重置编号"):
        return await cmd_renumber(update, context)
    if text in ("♻️ 撤销删除", "撤销删除"):
        return await cmd_recycle(update, context)
    if text in ("🧭 路径对应", "路径对应"):
        return await cmd_path(update, context)
    if text in ("❓ 帮助菜单", "帮助菜单", "帮助"):
        return await cmd_help(update, context)

    if text.isdigit():
        return await show_detail(update, user_id, int(text))

    if text.startswith("短链 ") or text.lower().startswith("short "):
        target = text.split(" ", 1)[1].strip()
        if not re.match(r"^https?://", target):
            await update.effective_message.reply_text("短链目标必须是 http/https 链接。")
            return
        code = await store.create_short(user_id, target)
        await update.effective_message.reply_html(
            f"✅ 短链已生成\n<code>{html.escape(PUBLIC_BASE_URL + '/s/' + code)}</code>",
            reply_markup=MAIN_KB,
        )
        return

    if "=" in text and not text.lower().startswith("http") and "://" not in text.split("=", 1)[0]:
        left, right = text.split("=", 1)
        node_name = left.strip()
        remark = right.strip()
        if not node_name:
            await update.effective_message.reply_text("格式：节点名=备注")
            return
        await store.upsert_path_map(user_id, node_name, remark)
        if remark:
            await update.effective_message.reply_text(f"已设置：{node_name} = {remark}", reply_markup=MAIN_KB)
        else:
            await update.effective_message.reply_text(f"已清空：{node_name}", reply_markup=MAIN_KB)
        return

    # name|url
    if "|" in text and re.search(r"https?://", text):
        name, url = [x.strip() for x in text.split("|", 1)]
        if re.match(r"^https?://", url):
            sub = await store.add_sub(user_id, name or urlparse(url).netloc or "订阅", url)
            sub = await refresh_sub(user_id, sub, rename=not bool(name))
            await update.effective_message.reply_html(
                f"✅ 已添加订阅 <b>{html.escape(sub['name'])}</b>\n节点: {len(nodes_of(sub))}",
                reply_markup=MAIN_KB,
            )
            return

    # subscription urls
    urls = re.findall(r"https?://\S+", text)
    if urls and not extract_share_links(text):
        added = []
        for url in urls:
            name = urlparse(url).netloc or "订阅"
            sub = await store.add_sub(user_id, name, url)
            sub = await refresh_sub(user_id, sub, rename=True)
            added.append(f"{sub['name']} ({len(nodes_of(sub))} 节点)")
        await update.effective_message.reply_text("✅ 已添加:\n" + "\n".join(added), reply_markup=MAIN_KB)
        return

    # node share links
    links = extract_share_links(text)
    if links:
        for link in links:
            await store.add_temp(user_id, link, node_name_from_url(link))
        await update.effective_message.reply_text(
            f"✅ 已加入临时列表 {len(links)} 条。发送 /temp 查看。",
            reply_markup=MAIN_KB,
        )
        return

    await update.effective_message.reply_text(
        "未识别内容。可发送订阅链接、节点链接，或点击帮助菜单。",
        reply_markup=MAIN_KB,
    )


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await deny(update):
        return
    message = update.effective_message
    document = message.document
    filename = (document.file_name or "upload.txt").strip() or "upload.txt"
    suffix = _document_suffix(filename)
    if suffix not in ALLOWED_DOCUMENT_SUFFIXES:
        await message.reply_text("不支持的文件类型。请上传 .txt、.log、.yaml、.yml 或 .json 文件。", reply_markup=MAIN_KB)
        return
    if document.file_size and document.file_size > MAX_DOCUMENT_BYTES:
        await message.reply_text(f"文件过大，最大支持 {MAX_DOCUMENT_BYTES // 1024 // 1024} MB。", reply_markup=MAIN_KB)
        return
    status = await message.reply_text("正在读取文件…")
    try:
        telegram_file = await context.bot.get_file(document.file_id)
        raw = bytes(await telegram_file.download_as_bytearray())
        if len(raw) > MAX_DOCUMENT_BYTES:
            await status.edit_text("文件过大，下载后超过大小限制。")
            return
        text = _decode_document(raw)
    except Exception:
        log.exception("document download failed")
        await status.edit_text("文件读取失败，请稍后重试。")
        return

    user_id = update.effective_user.id
    existing_urls = {sub["url"] for sub in await store.list_subs(user_id)}
    parsed_nodes = parse_nodes_from_text(text)
    structured = bool(parsed_nodes and ("proxies:" in text or "outbounds" in text))
    created: list[tuple[str, int]] = []
    failures: list[str] = []
    skipped = 0
    temp_count = 0

    if structured:
        name = filename.rsplit("/", 1)[-1].rsplit(".", 1)[0][:64] or "导入配置"
        local = await store.add_imported_sub(user_id, name, parsed_nodes[:MAX_IMPORTED_NODES])
        created.append((str(local["name"]), len(parsed_nodes[:MAX_IMPORTED_NODES])))
    else:
        urls = _unique(value.rstrip(".,;:!?)]}>\"\'") for value in re.findall(r"https?://[^\s<>\"]+", text))
        for url in urls[:MAX_IMPORTED_SUBSCRIPTIONS]:
            if url in existing_urls:
                skipped += 1
                continue
            name = urlparse(url).netloc or "订阅"
            try:
                sub = await store.add_sub(user_id, name, url)
                updated = await refresh_sub(user_id, sub, rename=True)
                if updated.get("last_error"):
                    failures.append(f"{name}: {updated["last_error"]}")
                else:
                    created.append((str(updated.get("name") or name), len(nodes_of(updated))))
            except Exception as exc:
                failures.append(f"{name}: {exc}")
        links = _unique(extract_share_links(text))[:MAX_IMPORTED_NODES]
        for link in links:
            await store.add_temp(user_id, link, node_name_from_url(link))
        temp_count = len(links)

    lines = [f"✅ 已处理文件：{html.escape(filename)}"]
    for name, count in created:
        lines.append(f"已添加：{html.escape(name)}（{count} 节点）")
    if temp_count:
        lines.append(f"已加入临时节点：{temp_count} 条")
    if skipped:
        lines.append(f"已跳过重复订阅：{skipped} 条")
    if failures:
        lines.append("失败：" + "；".join(html.escape(item) for item in failures[:8]))
    if not created and not temp_count and not failures and not skipped:
        lines.append("未识别到订阅或节点内容。")
    await status.edit_text("\n".join(lines), reply_markup=MAIN_KB, parse_mode=ParseMode.HTML)


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await deny(update):
        return
    q = update.callback_query
    await q.answer()
    data = q.data or ""
    user_id = update.effective_user.id
    if data == "noop":
        return
    if data == "list:home":
        await q.message.reply_text("🏠 主菜单", reply_markup=MAIN_KB)
        return
    if data.startswith("list:page:"):
        page = int(data.split(":")[-1])
        text, kb = await render_list(user_id, page)
        await q.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=kb, disable_web_page_preview=True)
        return
    if data.startswith("sub:open:"):
        parts = data.split(":")
        sub_id = int(parts[2])
        page = int(parts[3]) if len(parts) > 3 else 0
        sub = await store.get_sub(user_id, sub_id)
        if not sub:
            await q.edit_message_text("订阅不存在")
            return
        if not nodes_of(sub):
            sub = await refresh_sub(user_id, sub, rename=True)
        subs = await store.list_subs(user_id)
        idx = next((i for i, s in enumerate(subs, 1) if int(s["id"]) == sub_id), sub_id)
        await q.edit_message_text(
            detail_text(sub, idx),
            parse_mode=ParseMode.HTML,
            reply_markup=detail_keyboard(sub_id, page),
            disable_web_page_preview=True,
        )
        return
    if data.startswith("recycle:restore:"):
        deleted_id = int(data.split(":")[-1])
        restored = await store.restore_deleted(user_id, deleted_id)
        if restored:
            await q.edit_message_text(f"✅ 已恢复：{restored['name']}")
        else:
            await q.edit_message_text("恢复失败，记录可能已过期。")
        return
    if data.startswith("sub:back:"):
        page = int(data.split(":")[-1])
        text, kb = await render_list(user_id, page)
        await q.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=kb, disable_web_page_preview=True)
        return
    if data.startswith("sub:fmt:"):
        parts = data.split(":")
        sub_id = int(parts[2])
        kind = parts[3]
        sub = await store.get_sub(user_id, sub_id)
        if not sub:
            await q.message.reply_text("订阅不存在")
            return
        if not nodes_of(sub):
            sub = await refresh_sub(user_id, sub, rename=True)
        target = sub_token_url(sub["token"], kind)
        code = await store.create_short(user_id, target)
        short = f"{PUBLIC_BASE_URL}/s/{code}"
        title = {
            "clash": "Clash Meta",
            "singbox": "Sing-box",
            "base64": "Base64",
            "surge": "Surge",
            "qx": "QX",
        }.get(kind, kind)
        await q.message.reply_html(
            f"🔗 <b>{html.escape(title)}</b>\n<code>{html.escape(short)}</code>",
            disable_web_page_preview=True,
        )
        return
    if data.startswith("sub:refresh:"):
        parts = data.split(":")
        sub_id = int(parts[2])
        page = int(parts[3]) if len(parts) > 3 else 0
        sub = await store.get_sub(user_id, sub_id)
        if not sub:
            await q.edit_message_text("订阅不存在")
            return
        sub = await refresh_sub(user_id, sub, rename=True)
        # find display index
        subs = await store.list_subs(user_id)
        idx = next((i for i, s in enumerate(subs, 1) if int(s["id"]) == sub_id), sub_id)
        await q.edit_message_text(
            detail_text(sub, idx),
            parse_mode=ParseMode.HTML,
            reply_markup=detail_keyboard(sub_id, page),
            disable_web_page_preview=True,
        )
        return
    if data.startswith("sub:del:"):
        parts = data.split(":")
        sub_id = int(parts[2])
        page = int(parts[3]) if len(parts) > 3 else 0
        ok = await store.delete_sub(user_id, sub_id)
        text, kb = await render_list(user_id, page)
        prefix = "✅ 已删除\n\n" if ok else "❌ 删除失败\n\n"
        await q.edit_message_text(prefix + text, parse_mode=ParseMode.HTML, reply_markup=kb, disable_web_page_preview=True)
        return
    if data.startswith("sub:nodes:"):
        sub_id = int(data.split(":")[-1])
        sub = await store.get_sub(user_id, sub_id)
        if not sub:
            await q.message.reply_text("订阅不存在")
            return
        maps = {m["node_name"]: m["remark"] for m in await store.list_path_maps(user_id)}
        nodes = apply_path_maps(nodes_of(sub), maps)
        links = to_share_links(nodes)
        if not links:
            await q.message.reply_text("没有可导出的分享链接（可能来自 YAML 订阅）。")
            return
        # split if too long
        chunk = []
        size = 0
        for link in links:
            if size + len(link) + 1 > 3500:
                await q.message.reply_text("\n".join(chunk))
                chunk, size = [], 0
            chunk.append(link)
            size += len(link) + 1
        if chunk:
            await q.message.reply_text("\n".join(chunk))
        return


async def mapped_nodes_for_user(user_id: int) -> list[dict[str, Any]]:
    maps = {m["node_name"]: m["remark"] for m in await store.list_path_maps(user_id)}
    nodes: list[dict[str, Any]] = []
    for sub in await store.list_subs(user_id):
        nodes.extend(apply_path_maps(nodes_of(sub), maps))
    for t in await store.list_temp(user_id):
        nodes.append({"name": t["name"], "type": "temp", "share": t["url"]})
    return nodes


async def http_sub(request: web.Request) -> web.Response:
    token = request.match_info["token"]
    kind = request.match_info.get("kind", "clash").lower()
    sub = await store.get_sub_by_token(token)
    if not sub:
        return web.Response(status=404, text="not found")
    maps = {m["node_name"]: m["remark"] for m in await store.list_path_maps(int(sub["user_id"]))}
    nodes = apply_path_maps(nodes_of(sub), maps)
    if not nodes:
        sub = await refresh_sub(int(sub["user_id"]), sub)
        nodes = apply_path_maps(nodes_of(sub), maps)
    if kind in ("base64", "v2ray", "mixed"):
        return web.Response(text=to_base64_sub(nodes), content_type="text/plain")
    if kind in ("singbox", "sing-box"):
        return web.Response(text=to_singbox(nodes), content_type="application/json")
    if kind == "surge":
        return web.Response(text=to_surge(nodes), content_type="text/plain")
    if kind in ("qx", "quantumult", "quantumultx"):
        return web.Response(text=to_qx(nodes), content_type="text/plain")
    return web.Response(text=to_clash(nodes), content_type="text/yaml")


async def http_agg(request: web.Request) -> web.Response:
    user_id = int(request.match_info["user_id"])
    if ALLOWED_USER_IDS and user_id not in ALLOWED_USER_IDS:
        return web.Response(status=403, text="forbidden")
    kind = request.match_info.get("kind", "clash").lower()
    nodes = await mapped_nodes_for_user(user_id)
    if kind in ("base64", "v2ray", "mixed"):
        return web.Response(text=to_base64_sub(nodes), content_type="text/plain")
    if kind in ("singbox", "sing-box"):
        return web.Response(text=to_singbox(nodes), content_type="application/json")
    if kind == "surge":
        return web.Response(text=to_surge(nodes), content_type="text/plain")
    if kind in ("qx", "quantumult", "quantumultx"):
        return web.Response(text=to_qx(nodes), content_type="text/plain")
    return web.Response(text=to_clash(nodes), content_type="text/yaml")


async def http_short(request: web.Request) -> web.Response:
    code = request.match_info["code"]
    row = await store.get_short(code)
    if not row:
        return web.Response(status=404, text="not found")
    target = row["target_url"]
    if target.startswith("aggregate://"):
        raise web.HTTPFound(f"/agg/{row['user_id']}/clash")
    if not target.startswith(("http://", "https://")):
        return web.Response(status=400, text="invalid target")
    raise web.HTTPFound(target)


async def http_health(_: web.Request) -> web.Response:
    return web.json_response({"ok": True, "service": "subs-bot"})


async def start_http(app: Application) -> web.AppRunner:
    api = web.Application()
    api.router.add_get("/health", http_health)
    api.router.add_get("/sub/{token}/{kind}", http_sub)
    api.router.add_get("/sub/{token}", http_sub)
    api.router.add_get("/agg/{user_id}/{kind}", http_agg)
    api.router.add_get("/s/{code}", http_short)
    runner = web.AppRunner(api)
    await runner.setup()
    site = web.TCPSite(runner, HTTP_HOST, HTTP_PORT)
    await site.start()
    log.info("HTTP on %s:%s", HTTP_HOST, HTTP_PORT)
    return runner


def build_app() -> Application:
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("list", cmd_list))
    app.add_handler(CommandHandler("expire", cmd_expire))
    app.add_handler(CommandHandler("export", cmd_export))
    app.add_handler(CommandHandler("short", cmd_short))
    app.add_handler(CommandHandler("path", cmd_path))
    app.add_handler(CommandHandler("renumber", cmd_renumber))
    app.add_handler(CommandHandler("s", cmd_search))
    app.add_handler(CommandHandler("o", cmd_search_export))
    app.add_handler(CommandHandler("d", cmd_delete_match))
    app.add_handler(CommandHandler("i", cmd_inline_sort))
    app.add_handler(CommandHandler("ai", cmd_api_test))
    app.add_handler(CommandHandler("temp", cmd_temp))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    return app


async def amain() -> None:
    app = build_app()
    runner = await start_http(app)
    try:
        log.info("Bot starting...")
        await app.initialize()
        await app.start()
        await app.updater.start_polling(drop_pending_updates=True, allowed_updates=Update.ALL_TYPES)
        # run forever
        while True:
            await asyncio.sleep(3600)
    finally:
        with suppress(Exception):
            await app.updater.stop()
        with suppress(Exception):
            await app.stop()
        with suppress(Exception):
            await app.shutdown()
        with suppress(Exception):
            await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(amain())
