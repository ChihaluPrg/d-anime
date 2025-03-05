import discord
from discord.ext import tasks, commands
import requests
from bs4 import BeautifulSoup
import re
import json
import logging
import asyncio

# ログ設定はエラーのみ出力
logging.basicConfig(
    level=logging.ERROR,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)

# -------------------------------
# 複数のアニメ設定（各アニメごとに複数の通知先チャンネルに対応）
# -------------------------------
ANIME_CONFIGS = [
    {
        "name": "Re:ゼロから始める異世界生活 3rd season",
        "url": "https://animestore.docomo.ne.jp/animestore/ci_pc?workId=27314",
        "data_file": "last_episode_rezero.json",
        "target_channel_ids": [1346005112405098528, 1346017778515050617]
    },
    {
        "name": "ギルドの受付嬢ですが、残業は嫌なのでボスをソロ討伐しようと思います",
        "url": "https://animestore.docomo.ne.jp/animestore/ci_pc?workId=27622",
        "data_file": "last_episode_girudo.json",
        "target_channel_ids": [1346005112405098528, 1346017848690212905]
    }
]

# チェック間隔（秒） - 例：3600秒＝1時間
CHECK_INTERVAL = 3600

# Discord Bot のトークン（Developer Portalで取得）
DISCORD_BOT_TOKEN = "MTM0NjAwMDg1Mzg0ODU1OTYzNw.G7uIF8.3Ipc4k0ODQIGAPFbtjSf1oPK_1rn-V9MVO47pk"

# -------------------------------
# 共通のスクレイピング用関数
# -------------------------------
def extract_episode_num(text):
    """
    「第62話」または「#1」などの形式から番号部分を抽出して int 型で返す
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
    指定のURL（docomo アニメストアのページ）からエピソード一覧を解析し、
    最新のエピソード情報（番号・タイトル・リンク・サムネイル画像URL）を辞書形式で返す関数です。

    返り値の例:
      {
         "number": "第62話" または "#1",
         "title": "レグルス・コルニアス",
         "url": "https://animestore.docomo.ne.jp/animestore/ci_pc/cd_pc?partId=...",
         "thumbnail": "https://cs1.animestore.docomo.ne.jp/anime_kv/img/27/31/4/0/12/27314012_1_6.png?1740626619917"
      }
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

    # まずは従来のエピソード一覧用コンテナを探す
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

            # サムネイル画像取得: まず src 属性、なければ data-src 属性を確認
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
        logging.error("エピソードデータが取得できませんでした。")
        return None


# -------------------------------
# 状態管理用関数（各アニメごとにファイルで管理）
# -------------------------------
def load_state(data_file):
    data_file = os.path.join("/Users/kotohasaki/Documents/Py/d-anime", data_file)  # 絶対パスに変更
    if os.path.exists(data_file):
        try:
            with open(data_file, "r", encoding="utf-8") as f:
                content = f.read()
                if not content.strip():
                    return None
                data = json.loads(content)
                return data.get("state")
        except json.JSONDecodeError as e:
            logging.error(f"JSONデコードエラー in {data_file}: {e}")
            return None
    return None


def save_state(data_file, state):
    data_file = os.path.join("/Users/kotohasaki/Documents/Py/d-anime", data_file)  # 絶対パスに変更
    with open(data_file, "w", encoding="utf-8") as f:
        json.dump({"state": state}, f, ensure_ascii=False)


# -------------------------------
# Discord Bot のセットアップ
# -------------------------------
intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)


# hybrid command を利用することで、テキストコマンド(!c)とスラッシュコマンド(/c)の両方で実行可能
@commands.hybrid_command(name="c", with_app_command=True, description="14日以内のチャット履歴を全削除します。")
@commands.has_permissions(manage_messages=True)
async def clear_all(ctx: commands.Context):
    total_deleted = 0
    while True:
        # 1回の呼び出しで最大100件削除（ループで全件削除）
        deleted = await ctx.channel.purge(limit=100)
        if not deleted:
            break
        total_deleted += len(deleted)
        await asyncio.sleep(1)  # レート制限対策のため少し待機
    # テキストコマンドの場合はそのチャンネルに送信、スラッシュの場合は interaction.response を使用
    if isinstance(ctx, commands.Context):
        await ctx.send(f"合計 {total_deleted} 件のメッセージを削除しました", delete_after=5)
    else:
        await ctx.response.send_message(f"合計 {total_deleted} 件のメッセージを削除しました。", ephemeral=True)

bot.add_command(clear_all)

@tasks.loop(seconds=CHECK_INTERVAL)
async def check_anime_updates():
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/115.0.0.0 Safari/537.36"
        )
    }
    for anime in ANIME_CONFIGS:
        name = anime["name"]
        url = anime["url"]
        data_file = anime["data_file"]
        target_channel_ids = anime["target_channel_ids"]

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
        # 更新予定かどうかの判断：header 要素がなければコンプリートと見なす
        header_elem = soup.find("header", class_="attention onlyPcLayout")
        if header_elem is None:
            logging.error(f"[{name}] header が見つかりません。アニメがコンプリートされたと判断します。")
            if last_state == "complete":
                logging.error(f"[{name}] 既にコンプリート通知は送信済みです。")
            else:
                for channel_id in target_channel_ids:
                    channel = bot.get_channel(channel_id)
                    if channel is None:
                        logging.error(f"[{name}] 指定されたチャンネル {channel_id} が見つかりません")
                    else:
                        try:
                            await channel.send(f"{name} がコンプリートされました")
                        except Exception as e:
                            logging.error(f"[{name}] コンプリート通知送信エラー（チャンネル {channel_id}）: {e}")
                save_state(data_file, "complete")
            continue

        latest_episode = get_latest_episode(url)
        if latest_episode:
            new_episode_num = latest_episode["number"]
            if last_state != new_episode_num:
                for channel_id in target_channel_ids:
                    channel = bot.get_channel(channel_id)
                    if channel is None:
                        logging.error(f"[{name}] 指定されたチャンネル {channel_id} が見つかりません")
                    else:
                        try:
                            await channel.send(f"{name} の新しいエピソードが公開されました: {new_episode_num} {latest_episode['title']} {latest_episode['url']}")
                        except Exception as e:
                            logging.error(f"[{name}] 通知送信エラー（チャンネル {channel_id}）: {e}")
                save_state(data_file, new_episode_num)
            else:
                logging.info(f"[{name}] 最新エピソード {new_episode_num} は既に通知済みです。")
        else:
            logging.error(f"[{name}] 最新エピソード情報の取得に失敗しました。")

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user}")
    check_anime_updates.start()

bot.run(DISCORD_BOT_TOKEN)

import os

print("通知を送信します")  # デバッグ用
os.system('osascript -e \'display notification "通知の内容" with title "タイトル"\'')

