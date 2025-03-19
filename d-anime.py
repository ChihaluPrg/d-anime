import discord
from discord.ext import tasks, commands
import os
import json
import requests
import logging
import re
from bs4 import BeautifulSoup

# -------------------------------
# 基本設定
# -------------------------------
logging.basicConfig(
    level=logging.ERROR,  # 必要に応じ DEBUG/INFO に変更
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)

CHECK_INTERVAL = 3600   # 更新チェックの間隔（秒）
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# 各アニメの状態保存用ディレクトリ
DATA_DIR = os.path.join(BASE_DIR, "data")
os.makedirs(DATA_DIR, exist_ok=True)

# 保存先ディレクトリを絶対パスで指定
CONFIG_DIR = "/Users/kotohasaki/Documents/Py/d-anime"
os.makedirs(CONFIG_DIR, exist_ok=True)

# アニメ設定情報保存用ファイルのパスを更新
ANIME_CONFIG_FILE = os.path.join(CONFIG_DIR, "anime_configs.json")


# Bot のトークン（必ずご自身のものに置き換えてください）
DISCORD_BOT_TOKEN = "ここにトークン"

# 自動作成される専用チャンネルは、指定のカテゴリ内に作成（カテゴリID）
CATEGORY_ID = 123456789

# グローバル変数：アニメ設定情報リスト
anime_configs = []
# 例:
# [
#   {
#      "name": "いずれ最強の錬金術師？",
#      "url": "https://animestore.docomo.ne.jp/animestore/ci_pc?workId=XXXXX",
#      "data_file": "lastepisode_いずれ最強の錬金術師.json",
#      "target_channel_ids": [自動作成された専用チャンネルID, …]
#   },
#   …
# ]

# -------------------------------
# ヘルパー関数：安全な応答
# -------------------------------
async def safe_respond(ctx, content, ephemeral=False):
    # ephemeral の初期値を False に設定
    if hasattr(ctx, "interaction") and ctx.interaction:
        if not ctx.interaction.response.is_done():
            await ctx.interaction.response.send_message(content, ephemeral=ephemeral)
        else:
            # 直接送信（delete_after を設定しない）
            await ctx.send(content)
    else:
        await ctx.send(content)

def is_anime_ongoing(url):
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/115.0.0.0 Safari/537.36"
        )
    }
    try:
        response = requests.get(url, headers=headers)
    except Exception as e:
        logging.error(f"[{url}] HTML取得エラー: {e}")
        # エラー時は安全のため「追加不可」とする
        return False
    if response.status_code != 200:
        logging.error(f"[{url}] HTML取得失敗 (ステータスコード: {response.status_code})")
        return False
    soup = BeautifulSoup(response.text, "html.parser")
    # <p class="note schedule">～更新予定</p> が存在すれば未完結とみなす
    note_schedule = soup.find("p", class_="note schedule")
    if note_schedule and "更新予定" in note_schedule.get_text():
        return True
    return False


# -------------------------------
# 状態管理用関数
# -------------------------------
def load_state(data_file):
    file_path = os.path.join(DATA_DIR, data_file)
    if os.path.exists(file_path):
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                content = f.read()
                if not content.strip():
                    return None
                data = json.loads(content)
                return data.get("state")
        except json.JSONDecodeError as e:
            logging.error(f"{file_path} の JSON デコードエラー: {e}")
            return None
    return None

def save_state(data_file, state):
    file_path = os.path.join(DATA_DIR, data_file)
    with open(file_path, "w", encoding="utf-8") as f:
        json.dump({"state": state}, f, ensure_ascii=False)

# -------------------------------
# アニメ設定管理用関数
# -------------------------------
def load_anime_configs():
    global anime_configs
    if os.path.exists(ANIME_CONFIG_FILE):
        try:
            with open(ANIME_CONFIG_FILE, "r", encoding="utf-8") as f:
                anime_configs[:] = json.load(f)
        except Exception as e:
            logging.error(f"アニメ設定の読み込みエラー: {e}")
            anime_configs[:] = []
    else:
        anime_configs[:] = []

def save_anime_configs():
    try:
        with open(ANIME_CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(anime_configs, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logging.error(f"アニメ設定の保存エラー: {e}")

# -------------------------------
# スクレイピング関連の関数
# -------------------------------
def extract_episode_num(text):
    m = re.search(r"第(\d+)話", text)
    if m:
        return int(m.group(1))
    m = re.search(r"#(\d+)", text)
    if m:
        return int(m.group(1))
    return None

def get_latest_episode(url):
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/115.0.0.0 Safari/537.36"
        )
    }
    try:
        response = requests.get(url, headers=headers)
    except Exception as e:
        logging.error(f"[{url}] ページ取得エラー: {e}")
        return None
    if response.status_code != 200:
        logging.error(f"[{url}] ページ取得失敗 (ステータスコード: {response.status_code})")
        return None
    soup = BeautifulSoup(response.text, "html.parser")
    container = soup.select_one("div.episodeContainer.itemWrapper.swiper-wrapper")
    if container:
        episodes = container.find_all("a", id=lambda x: x and x.startswith("episodePartId"))
    else:
        episodes = soup.select("div.itemModule.list a[id^='episodePartId']")
    if not episodes:
        logging.error("エピソード要素が見つかりませんでした。")
        return None
    latest_data = None  # (数値, 番号テキスト, タイトル, リンク, サムネイル)
    for ep in episodes:
        number_span = ep.find("span", class_="number")
        title_h3 = ep.find("h3", class_="line2")
        if number_span and title_h3:
            ep_number_text = number_span.get_text(strip=True)
            ep_number = extract_episode_num(ep_number_text)
            title = title_h3.get_text(strip=True)
            href = ep.get("href")
            base_url = "https://animestore.docomo.ne.jp/animestore/ci_pc/"
            full_url = href if href.startswith("http") else base_url + href
            img_tag = ep.find("img")
            thumbnail = None
            if img_tag:
                if img_tag.has_attr("src") and img_tag["src"].strip():
                    thumbnail = img_tag["src"].strip()
                elif img_tag.has_attr("data-src") and img_tag["data-src"].strip():
                    thumbnail = img_tag["data-src"].strip()
            if ep_number is not None:
                if (latest_data is None) or (ep_number > latest_data[0]):
                    latest_data = (ep_number, ep_number_text, title, full_url, thumbnail)
    if latest_data:
        return {
            "number": latest_data[1],
            "title": latest_data[2],
            "url": latest_data[3],
            "thumbnail": latest_data[4]
        }
    else:
        logging.error("最新エピソード情報が取得できませんでした。")
        return None

# -------------------------------
# Discord Bot のセットアップ (グローバル同期のみ)
# -------------------------------
intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)

# -------------------------------
# アニメ設定管理コマンドグループ (/anime)
# -------------------------------
@commands.hybrid_group(
    name="anime",
    with_app_command=True,
    description="アニメ設定管理"
)
@commands.has_permissions(administrator=True)
async def anime(ctx):
    if ctx.invoked_subcommand is None:
        await safe_respond(ctx, "使用可能な subcommand: list, add, remove", ephemeral=True)

@anime.command(
    name="list",
    with_app_command=True,
    description="登録アニメ一覧表示"
)
async def anime_list(ctx):
    if not anime_configs:
        msg = "アニメ設定は未登録です。"
    else:
        msg_lines = ["**登録アニメ設定一覧:**"]
        for idx, conf in enumerate(anime_configs, start=1):
            channels = ", ".join(str(cid) for cid in conf.get("target_channel_ids", []))
            msg_lines.append(
                f"{idx}. **{conf.get('name')}**\n"
                f"   URL: {conf.get('url')}\n"
                f"   Data file: {conf.get('data_file')}\n"
                f"   チャンネルID: {channels}"
            )
        msg = "\n".join(msg_lines)
    await safe_respond(ctx, msg, ephemeral=False)



@anime.command(
    name="add",
    with_app_command=True,
    description="新アニメ設定追加（専用チャンネル作成・初回通知）"
)
async def anime_add(ctx, name: str, url: str, data_file: str = None, channels: commands.Greedy[discord.TextChannel] = None):
    if ctx.guild is None:
        await safe_respond(ctx, "このコマンドはサーバー内でのみ使用可能です。", ephemeral=False)
        return
    for conf in anime_configs:
        if conf.get("name").lower() == name.lower():
            await safe_respond(ctx, f"**{name}** は既に追加済みです。", ephemeral=False)
            return

    # 追加前に完結済み（更新予定情報がない）かチェック
    if not is_anime_ongoing(url):
        await safe_respond(ctx, f"**{name}** は完結済みのアニメのため追加できません。", ephemeral=False)
        return

    try:
        category = ctx.guild.get_channel(CATEGORY_ID)
        if category is None:
            await safe_respond(ctx, "指定されたカテゴリが見つかりません。", ephemeral=False)
            return
        auto_channel = await ctx.guild.create_text_channel(name, category=category, reason="自動作成: 専用通知チャンネル")
    except Exception as e:
        await safe_respond(ctx, f"専用チャンネルの作成に失敗しました: {e}", ephemeral=False)
        return

    # 自動作成された専用チャンネルIDをリストに追加
    target_channel_ids = [auto_channel.id]

    # オプションで指定されたチャンネルがあれば追加
    if channels is not None:
        for ch in channels:
            if ch.id not in target_channel_ids:
                target_channel_ids.append(ch.id)

    # 追加通知用のチャンネルID（例: 1346005112405098528）を必ず追加
    additional_notify_channel_id = 1346005112405098528
    if additional_notify_channel_id not in target_channel_ids:
        target_channel_ids.append(additional_notify_channel_id)

    if data_file is None:
        safe_name = re.sub(r'\W+', '', name.lower())
        data_file = f"last_episode_{safe_name}.json"
    new_conf = {
        "name": name,
        "url": url,
        "data_file": data_file,
        "target_channel_ids": target_channel_ids
    }
    anime_configs.append(new_conf)
    save_anime_configs()

    latest = get_latest_episode(url)
    if latest:
        auto_notify_msg = (
            f"**{name}** の最新エピソード:\n"
            f"{latest['number']} {latest['title']}\n"
            f"URL: {latest['url']}"
        )
        try:
            await auto_channel.send(auto_notify_msg)
        except Exception as e:
            logging.error(f"専用チャンネル通知送信エラー: {e}")
        addition_msg = (
            f"**{name}** を追加しました。\n"
            f"専用チャンネル: {auto_channel.mention}\n"
            f"最新エピソード: {latest['number']} {latest['title']}\n"
            f"URL: {latest['url']}"
        )
    else:
        addition_msg = (
            f"**{name}** を追加しましたが、最新エピソード情報は取得できませんでした。\n"
            f"専用チャンネル: {auto_channel.mention}"
        )
    await safe_respond(ctx, addition_msg, ephemeral=False)




@anime.command(
    name="remove",
    with_app_command=True,
    description="指定アニメ設定削除（専用チャンネルも削除）"
)
async def anime_remove(ctx, name: str):
    global anime_configs
    removed_conf = None
    for conf in anime_configs:
        if conf.get("name").lower() == name.lower():
            removed_conf = conf
            anime_configs.remove(conf)
            break
    if removed_conf:
        save_anime_configs()
        if "target_channel_ids" in removed_conf and len(removed_conf["target_channel_ids"]) > 0:
            auto_channel_id = removed_conf["target_channel_ids"][0]
            auto_channel = ctx.guild.get_channel(auto_channel_id)
            if auto_channel:
                try:
                    await auto_channel.delete(reason="設定削除に伴い専用チャンネルを削除")
                except Exception as e:
                    logging.error(f"専用チャンネル削除エラー: {e}")
        msg = f"**{name}** の設定を削除しました。"
        await safe_respond(ctx, msg, ephemeral=False)
    else:
        msg = f"**{name}** の設定は見つかりませんでした。"
        await safe_respond(ctx, msg, ephemeral=False)

@anime_remove.autocomplete("name")
async def anime_remove_autocomplete(interaction: discord.Interaction, current: str):
    load_anime_configs()  # 最新情報を反映する
    return [
        discord.app_commands.Choice(name=conf["name"], value=conf["name"])
        for conf in anime_configs if current.lower() in conf["name"].lower()
    ]



bot.add_command(anime)

# -------------------------------
# バックグラウンドタスク：アニメ更新チェック
# -------------------------------
@tasks.loop(seconds=CHECK_INTERVAL)
async def check_anime_updates():
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/115.0.0.0 Safari/537.36"
        )
    }
    # 指定の完結済み用カテゴリを取得
    completed_category = bot.get_channel(1347062290641457214)
    if completed_category is None:
        logging.error("完結済み用カテゴリ (ID: 1347062290641457214) が見つかりません。")

    for anime_conf in anime_configs:
        name = anime_conf["name"]
        url = anime_conf["url"]
        data_file = anime_conf["data_file"]
        target_channel_ids = anime_conf["target_channel_ids"]

        # まずは、完結しているかをチェック
        if not is_anime_ongoing(url):
            # 完結している場合、専用チャンネル（先頭のID）を完結用カテゴリに移動
            if target_channel_ids:
                dedicated_channel_id = target_channel_ids[0]
                channel = bot.get_channel(dedicated_channel_id)
                if channel and completed_category and channel.category_id != completed_category.id:
                    try:
                        await channel.edit(category=completed_category, reason="アニメ完結に伴い専用チャンネルの移動")
                        logging.info(f"[{name}] 専用チャンネルを完結カテゴリに移動しました。")
                    except Exception as e:
                        logging.error(f"[{name}] 専用チャンネル移動エラー: {e}")
            # 完結アニメは新エピソード更新もないため、ここでスキップ
            continue

        # アニメが未完結の場合、通常の新エピソード更新チェックを実施
        last_state = load_state(data_file)
        try:
            response = requests.get(url, headers=headers)
        except Exception as e:
            logging.error(f"[{name}] ページ取得エラー: {e}")
            continue
        if response.status_code != 200:
            logging.error(f"[{name}] ページ取得失敗 (ステータスコード: {response.status_code})")
            continue

        latest_episode = get_latest_episode(url)
        if latest_episode:
            new_episode_num = latest_episode["number"]
            if last_state != new_episode_num:
                for channel_id in target_channel_ids:
                    channel = bot.get_channel(channel_id)
                    if channel:
                        try:
                            await channel.send(
                                f"**{name}** の新エピソード:\n"
                                f"{new_episode_num} {latest_episode['title']}\n"
                                f"{latest_episode['url']}"
                            )
                        except Exception as e:
                            logging.error(f"[{name}] 通知送信エラー (チャンネル {channel_id}): {e}")
                    else:
                        logging.error(f"[{name}] チャンネル {channel_id} が見つかりません。")
                save_state(data_file, new_episode_num)
            else:
                logging.info(f"[{name}] {new_episode_num} は既に通知済みです。")
        else:
            logging.error(f"[{name}] 最新エピソード情報の取得に失敗しました。")


# -------------------------------
# Bot 起動時の処理
# -------------------------------
@bot.event
async def on_ready():
    load_anime_configs()
    try:
        await bot.tree.sync()
    except discord.Forbidden as e:
        logging.error(f"Globalコマンド同期エラー: {e}")
    print(f"Bot {bot.user} としてログインしました！")
    check_anime_updates.start()

bot.run(DISCORD_BOT_TOKEN)
