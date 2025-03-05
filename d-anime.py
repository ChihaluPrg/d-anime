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
# Basic setup and constants
# -------------------------------

# Set logging to only show errors.
logging.basicConfig(
    level=logging.ERROR,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)

# Directories and file names
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
os.makedirs(DATA_DIR, exist_ok=True)  # ensure the data folder exists

ANIME_CONFIG_FILE = os.path.join(BASE_DIR, "anime_configs.json")

# Interval (in seconds) between anime checks — e.g., 3600s = 1 hour.
CHECK_INTERVAL = 3600

# Discord Bot token (replace with your token)
DISCORD_BOT_TOKEN = "MTM0NjAwMDg1Mzg0ODU1OTYzNw.G7uIF8.3Ipc4k0ODQIGAPFbtjSf1oPK_1rn-V9MVO47pk"

# Global in-memory anime configuration (loaded from file)
# Each anime config is a dict with keys: name, url, data_file, target_channel_ids (list of ints)
anime_configs = []  # will be loaded from file below


# -------------------------------
# Helper functions for state files
# -------------------------------

def load_state(data_file):
    """Load the saved last episode state from a JSON file in DATA_DIR."""
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
            logging.error(f"JSON decode error in {file_path}: {e}")
            return None
    return None


def save_state(data_file, state):
    """Save the new state (such as the latest episode number or 'complete') into a JSON file in DATA_DIR."""
    file_path = os.path.join(DATA_DIR, data_file)
    with open(file_path, "w", encoding="utf-8") as f:
        json.dump({"state": state}, f, ensure_ascii=False)


# -------------------------------
# Helper functions for anime configuration
# -------------------------------

def load_anime_configs():
    """Load the list of anime configurations from the JSON file."""
    global anime_configs
    if os.path.exists(ANIME_CONFIG_FILE):
        try:
            with open(ANIME_CONFIG_FILE, "r", encoding="utf-8") as f:
                anime_configs[:] = json.load(f)
        except Exception as e:
            logging.error(f"Error loading anime configurations: {e}")
            anime_configs[:] = []
    else:
        anime_configs[:] = []  # start with an empty list if config file does not exist


def save_anime_configs():
    """Save the global anime_configs list into the JSON file."""
    try:
        with open(ANIME_CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(anime_configs, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logging.error(f"Error saving anime configurations: {e}")


# -------------------------------
# Scraping functions
# -------------------------------

def extract_episode_num(text):
    """
    Extract the numeric portion from text such as "第62話" or "#1"
    and return as int.
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
    Scrape the given anime page URL and return the latest episode details
    as a dictionary with keys: number, title, url, thumbnail.
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
        logging.error(f"[{url}] Page fetch error: {e}")
        return None
    if response.status_code != 200:
        logging.error(f"[{url}] Page fetch failed (status code: {response.status_code})")
        return None

    soup = BeautifulSoup(response.text, "html.parser")

    # Try to locate the episode list container
    container = soup.select_one("div.episodeContainer.itemWrapper.swiper-wrapper")
    if container:
        episodes = container.find_all("a", id=lambda x: x and x.startswith("episodePartId"))
    else:
        episodes = soup.select("div.itemModule.list a[id^='episodePartId']")

    if not episodes:
        logging.error("No episode elements found.")
        return None

    latest_data = None  # (episode_num, episode_text, title, link, thumbnail)
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

            # Get thumbnail image URL from "src" or "data-src"
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
        logging.error("Could not retrieve episode data.")
        return None


# -------------------------------
# Setting up the Discord Bot
# -------------------------------

intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)


# -------------------------------
# A simple clear chat command (hybrid command)
# -------------------------------
@commands.hybrid_command(
    name="c",
    with_app_command=True,
    description="Deletes recent chat history (messages within the past 14 days)."
)
@commands.has_permissions(manage_messages=True)
async def clear_all(ctx: commands.Context):
    total_deleted = 0
    while True:
        # Delete up to 100 messages per batch in a loop until no messages are returned.
        deleted = await ctx.channel.purge(limit=100)
        if not deleted:
            break
        total_deleted += len(deleted)
        await asyncio.sleep(1)  # wait a bit to avoid rate limits
    await ctx.send(f"Deleted a total of {total_deleted} messages.", delete_after=5)


bot.add_command(clear_all)


# -------------------------------
# Anime configuration commands group
# -------------------------------

@commands.hybrid_group(name="anime", with_app_command=True, description="Manage anime notification settings.")
@commands.has_permissions(administrator=True)
async def anime(ctx):
    if ctx.invoked_subcommand is None:
        await ctx.send("Available subcommands: `list`, `add`, `remove`.")


@anime.command(name="list", with_app_command=True, description="List all configured animes.")
async def anime_list(ctx):
    if not anime_configs:
        await ctx.send("No anime configurations found.")
        return
    msg_lines = ["**Anime Configurations:**"]
    for idx, conf in enumerate(anime_configs, start=1):
        channels = ", ".join(str(cid) for cid in conf.get("target_channel_ids", []))
        msg_lines.append(
            f"{idx}. **{conf.get('name')}**\n   URL: {conf.get('url')}\n   Data file: {conf.get('data_file')}\n   Channel IDs: {channels}")
    await ctx.send("\n".join(msg_lines))


@anime.command(name="add", with_app_command=True, description="Add a new anime notification configuration.")
async def anime_add(ctx, name: str, url: str, data_file: str = None,
                    channels: commands.Greedy[discord.TextChannel] = None):
    """
    Add a new anime configuration.

    Parameters:
      • name: The name/title of the anime.
      • url: The url to the anime's page.
      • data_file (optional): Filename to store the last notified episode (if omitted, one is generated).
      • channels: One or more channels (as mentions) to send notifications.
    """
    if channels is None or len(channels) == 0:
        await ctx.send("You must specify at least one channel for notifications (mention the channel).")
        return

    # generate a safe file name if data_file is not provided
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
    await ctx.send(f"Anime configuration for **{name}** added successfully.")


@anime.command(name="remove", with_app_command=True, description="Remove an anime configuration by name.")
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
        await ctx.send(f"Anime configuration for **{name}** has been removed.")
    else:
        await ctx.send(f"Could not find an anime configuration matching **{name}**.")


bot.add_command(anime)


# -------------------------------
# Background Task: Check for anime updates
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
    # For each configured anime, check for updates.
    for anime_conf in anime_configs:
        name = anime_conf["name"]
        url = anime_conf["url"]
        data_file = anime_conf["data_file"]
        target_channel_ids = anime_conf["target_channel_ids"]

        last_state = load_state(data_file)

        try:
            response = requests.get(url, headers=headers)
        except Exception as e:
            logging.error(f"[{name}] Error fetching page: {e}")
            continue

        if response.status_code != 200:
            logging.error(f"[{name}] Failed to fetch page (status code: {response.status_code}).")
            continue

        soup = BeautifulSoup(response.text, "html.parser")
        # Check whether the anime appears to be updating.
        header_elem = soup.find("header", class_="attention onlyPcLayout")
        if header_elem is None:
            logging.error(f"[{name}] Header not found. Treating anime as complete.")
            if last_state == "complete":
                logging.error(f"[{name}] Completion notification already sent.")
            else:
                for channel_id in target_channel_ids:
                    channel = bot.get_channel(channel_id)
                    if channel is None:
                        logging.error(f"[{name}] Could not find channel {channel_id}.")
                    else:
                        try:
                            await channel.send(f"{name} has been completed.")
                        except Exception as e:
                            logging.error(f"[{name}] Error sending complete notification (channel {channel_id}): {e}")
                save_state(data_file, "complete")
            continue

        latest_episode = get_latest_episode(url)
        if latest_episode:
            new_episode_num = latest_episode["number"]
            if last_state != new_episode_num:
                for channel_id in target_channel_ids:
                    channel = bot.get_channel(channel_id)
                    if channel is None:
                        logging.error(f"[{name}] Could not find channel {channel_id}.")
                    else:
                        try:
                            await channel.send(
                                f"{name} has a new episode: {new_episode_num} {latest_episode['title']}\n{latest_episode['url']}"
                            )
                        except Exception as e:
                            logging.error(f"[{name}] Error sending notification (channel {channel_id}): {e}")
                save_state(data_file, new_episode_num)
            else:
                logging.info(f"[{name}] Episode {new_episode_num} already notified.")
        else:
            logging.error(f"[{name}] Failed to retrieve latest episode information.")


@bot.event
async def on_ready():
    # Load anime configurations from file on startup.
    load_anime_configs()
    print(f"Logged in as {bot.user}")
    # Start the update checking task.
    check_anime_updates.start()


bot.run(DISCORD_BOT_TOKEN)
