import discord
from discord.ext import tasks, commands
import os
import json
import requests
import logging
import asyncio
import re
from bs4 import BeautifulSoup

# -------------------------------
# 基本設定
# -------------------------------

# ログ設定（エラーのみ表示）
logging.basicConfig(
    level=logging.ERROR,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)

# 定期チェック間隔（秒）: 例 3600 秒 = 1 時間
CHECK_INTERVAL = 3600

# プロジェクトの基準ディレクトリ
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# 状態保存用（各アニメの最新エピソード番号や完結状態を格納する）のディレクトリ
DATA_DIR = os.path.join(BASE_DIR, "data")
os.makedirs(DATA_DIR, exist_ok=True)

# アニメ設定情報を管理する JSON ファイル
ANIME_CONFIG_FILE = os.path.join(BASE_DIR, "anime_configs.json")

# Discord Bot のトークン（自分のトークンに置き換えてください）
DISCORD_BOT_TOKEN = "MTM0NjAwMDg1Mzg0ODU1OTYzNw.G7uIF8.3Ipc4k0ODQIGAPFbtjSf1oPK_1rn-V9MVO47pk"

# グローバル変数にアニメ設定情報（リスト）を格納する
anime_configs = []  # 例： [{"name": ..., "url": ..., "data_file": ..., "target_channel_ids": [...]}, ...]

# -------------------------------
# 状態管理用関数
# -------------------------------

def load_state(data_file):
    """
    DATA_DIR 内の指定ファイルから、状態（例：最後に通知済みのエピソード番号や完結状態）を読み込む。
    """
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
    """
    DATA_DIR 内の指定ファイルに、新しい状態を保存する。
    """
    file_path = os.path.join(DATA_DIR, data_file)
    with open(file_path, "w", encoding="utf-8") as f:
        json.dump({"state": state}, f, ensure_ascii=False)

# -------------------------------
# アニメ設定（コンフィグ）管理用関数
# -------------------------------

def load_anime_configs():
    """
    ANIME_CONFIG_FILE からアニメ設定情報を読み込み、グローバル変数 anime_configs にセットする。
    """
    global anime_configs
    if os.path.exists(ANIME_CONFIG_FILE):
        try:
            with open(ANIME_CONFIG_FILE, "r", encoding="utf-8") as f:
                anime_configs[:] = json.load(f)
        except Exception as e:
            logging.error(f"アニメ設定の読み込みエラー: {e}")
            anime_configs[:] = []
    else:
        anime_configs[:] = []  # ファイルがなければ空リスト

def save_anime_configs():
    """
    グローバル変数 anime_configs を ANIME_CONFIG_FILE に保存する。
    """
    try:
        with open(ANIME_CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(anime_configs, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logging.error(f"アニメ設定の保存エラー: {e}")

# -------------------------------
# スクレイピング関連の関数
# -------------------------------

def extract_episode_num(text):
    """
    「第62話」や「#1」などのテキストから数値部分を抽出し、int で返す。
    """
    m = re.search(r"第(\d+)話", text)
    if m:
        return int(m.group(1))
    m = re.search(r"#(\d+)", text)
    if m:
        return int(m.group(1))
    return None

def get_latest_episode(url):
    """
    指定された URL のアニメページから最新エピソードの情報（番号、タイトル、リンク、サムネイル）を辞書で返す。
    """
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

    # エピソード一覧がある要素を探す
    container = soup.select_one("div.episodeContainer.itemWrapper.swiper-wrapper")
    if container:
        episodes = container.find_all("a", id=lambda x: x and x.startswith("episodePartId"))
    else:
        episodes = soup.select("div.itemModule.list a[id^='episodePartId']")

    if not episodes:
        logging.error("エピソード要素が見つかりませんでした。")
        return None

    latest_data = None  # (エピソード番号, 番号テキスト, タイトル, リンク, サムネイル)
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

            # サムネイル画像の取得
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
# Discord Bot のセットアップ
# -------------------------------

intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)

# ----------------------------------
# 補助コマンド：チャット履歴一括削除
# ----------------------------------
@commands.hybrid_command(
    name="c",
    with_app_command=True,
    description="過去14日以内のメッセージを全削除します。"
)
@commands.has_permissions(manage_messages=True)
async def clear_all(ctx: commands.Context):
    total_deleted = 0
    while True:
        deleted = await ctx.channel.purge(limit=100)
        if not deleted:
            break
        total_deleted += len(deleted)
        await asyncio.sleep(1)  # レート制限対策
    await ctx.send(f"{total_deleted} 件のメッセージを削除しました。", delete_after=5)

bot.add_command(clear_all)

# -------------------------------
# アニメ設定管理のコマンド群 (/anime)
# -------------------------------

@commands.hybrid_group(
    name="anime",
    with_app_command=True,
    description="アニメ通知設定を管理します。（サブコマンド: list, add, remove）"
)
@commands.has_permissions(administrator=True)
async def anime(ctx):
    if ctx.invoked_subcommand is None:
        await ctx.send("使用可能なサブコマンド: `list`, `add`, `remove`")

@anime.command(
    name="list",
    with_app_command=True,
    description="現在登録されているアニメ設定一覧を表示します。"
)
async def anime_list(ctx):
    if not anime_configs:
        await ctx.send("アニメの設定は登録されていません。")
        return

    msg_lines = ["**登録されているアニメ設定:**"]
    for idx, conf in enumerate(anime_configs, start=1):
        channels = ", ".join(str(cid) for cid in conf.get("target_channel_ids", []))
        msg_lines.append(f"{idx}. **{conf.get('name')}**\n   URL: {conf.get('url')}\n   Data file: {conf.get('data_file')}\n   チャンネルID: {channels}")
    await ctx.send("\n".join(msg_lines))

@anime.command(
    name="add",
    with_app_command=True,
    description="新たなアニメ通知設定を追加します。"
)
async def anime_add(ctx, name: str, url: str, data_file: str = None, channels: commands.Greedy[discord.TextChannel] = None):
    """
    新しいアニメ設定を追加するためのコマンドです。

    パラメーター:
      • name: アニメの名称
      • url: アニメページの URL
      • data_file (任意): 通知状態を保存するファイル名（指定がない場合自動生成）
      • channels: 通知先のチャンネル（複数指定可能）
    """
    if channels is None or len(channels) == 0:
        await ctx.send("通知先チャンネルを1つ以上指定してください。（チャンネルのメンションを利用）")
        return

    # data_file の指定がない場合は、自動的に安全なファイル名を生成
    if data_file is None:
        safe_name = re.sub(r'\W+', '', name.lower())
        data_file = f"last_episode_{safe_name}.json"

    new_conf = {
        "name": name,
        "url": url,
        "data_file": data_file,
        "target_channel_ids": [channel.id for channel in channels]
    }
    anime_configs.append(new_conf)
    save_anime_configs()
    await ctx.send(f"**{name}** の設定を追加しました。")

@anime.command(
    name="remove",
    with_app_command=True,
    description="指定したアニメの通知設定を削除します。"
)
async def anime_remove(ctx, name: str):
    global anime_configs
    removed = False
    for conf in anime_configs:
        if conf.get("name").lower() == name.lower():
            anime_configs.remove(conf)
            removed = True
            break
    if removed:
        save_anime_configs()
        await ctx.send(f"**{name}** の設定を削除しました。")
    else:
        await ctx.send(f"**{name}** に該当する設定が見つかりませんでした。")

bot.add_command(anime)

# -------------------------------
# バックグラウンドタスク：アニメ更新のチェック
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

    for anime_conf in anime_configs:
        name = anime_conf["name"]
        url = anime_conf["url"]
        data_file = anime_conf["data_file"]
        target_channel_ids = anime_conf["target_channel_ids"]

        last_state = load_state(data_file)

        try:
            response = requests.get(url, headers=headers)
        except Exception as e:
            logging.error(f"[{name}] ページ取得エラー: {e}")
            continue

        if response.status_code != 200:
            logging.error(f"[{name}] ページ取得失敗 (ステータスコード: {response.status_code})")
            continue

        soup = BeautifulSoup(response.text, "html.parser")
        # ページ内に更新情報用のヘッダーが存在しない場合、アニメは「完結」と見做す
        header_elem = soup.find("header", class_="attention onlyPcLayout")
        if header_elem is None:
            logging.error(f"[{name}] ヘッダーが見つかりません。アニメが完結していると判断します。")
            if last_state == "complete":
                logging.error(f"[{name}] 完結通知は既に送信済みです。")
            else:
                for channel_id in target_channel_ids:
                    channel = bot.get_channel(channel_id)
                    if channel is None:
                        logging.error(f"[{name}] チャンネル {channel_id} が見つかりません。")
                    else:
                        try:
                            await channel.send(f"**{name}** が完結しました。")
                        except Exception as e:
                            logging.error(f"[{name}] 完結通知送信エラー (チャンネル {channel_id}): {e}")
                save_state(data_file, "complete")
            continue

        latest_episode = get_latest_episode(url)
        if latest_episode:
            new_episode_num = latest_episode["number"]
            # 既に通知済みでなければ、通知を送信
            if last_state != new_episode_num:
                for channel_id in target_channel_ids:
                    channel = bot.get_channel(channel_id)
                    if channel is None:
                        logging.error(f"[{name}] チャンネル {channel_id} が見つかりません。")
                    else:
                        try:
                            await channel.send(
                                f"**{name}** の新エピソードが公開されました！\n"
                                f"{new_episode_num} {latest_episode['title']}\n{latest_episode['url']}"
                            )
                        except Exception as e:
                            logging.error(f"[{name}] 通知送信エラー (チャンネル {channel_id}): {e}")
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
    load_anime_configs()  # 起動時にアニメ設定情報を読み込む
    print(f"Bot {bot.user} としてログインしました！")
    check_anime_updates.start()  # 定期タスクを開始

bot.run(DISCORD_BOT_TOKEN)
