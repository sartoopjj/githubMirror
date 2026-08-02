#!/usr/bin/env python3

import os
import json
import hashlib
import asyncio
from html import escape as html_escape
import logging
import time
from datetime import datetime
from typing import Dict, List, Optional
from packaging.version import Version

from bs4 import BeautifulSoup
import cloudscraper

from telethon import TelegramClient, events, Button
from telethon.tl.functions.messages import GetDialogsRequest
from telethon.tl.types import InputPeerEmpty
from telethon.tl.types import InputMediaDocument

# Load environment variables from .env file
def load_env():
    env_vars = {}
    if os.path.exists('.env'):
        with open('.env', 'r') as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith('#') and '=' in line:
                    key, value = line.split('=', 1)
                    env_vars[key.strip()] = value.strip()
    return env_vars

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

class Config:
    def __init__(self):
        self.telegram = {}
        self.repositories = []

class Repository:
    def __init__(self):
        self.name = ""
        self.github_url = ""
        self.google_play_url = ""
        self.apple_store_url = ""
        self.microsoft_store_url = ""
        # TestFlight is a beta channel, not the App Store — kept separate so
        # apple_store_url can take over once a store build ships.
        self.testflight_url = ""

class APKMirror:
    def __init__(self, timeout: int = 5, results: int = 5):
        self.timeout = timeout
        self.results = results
        self.user_agent = "Mozilla/5.0 (X11; Linux x86_64; rv:122.0) Gecko/20100101 Firefox/122.0"
        self.headers = {"User-Agent": self.user_agent}
        self.base_url = "https://www.apkmirror.com"
        self.base_search = f"{self.base_url}/?post_type=app_release&searchtype=apk&s="
        self.scraper = cloudscraper.create_scraper()

    def search(self, query):
        time.sleep(self.timeout)
        search_url = self.base_search + query.replace('.', '+')
        resp = self.scraper.get(search_url, headers=self.headers)
        soup = BeautifulSoup(resp.text, "html.parser")
        apps = []
        appRow = soup.find_all("div", {"class": "appRow"})
        for app in appRow:
            try:
                app_dict = {
                    "name": app.find("h5", {"class": "appRowTitle"}).text.strip(),
                    "link": self.base_url + app.find("a", {"class": "downloadLink"})["href"],
                }
                apps.append(app_dict)
            except AttributeError:
                pass
        logger.info(f"APKMirror search for '{query}' found {len(apps)} results")
        return apps[:self.results]

    def get_latest_version_link(self, app_link):
        time.sleep(self.timeout)
        resp = self.scraper.get(app_link, headers=self.headers)
        soup = BeautifulSoup(resp.text, "html.parser")
        # The latest version is the first .appRow in the list
        appRow = soup.find("div", {"class": "appRow"})
        if appRow:
            return self.base_url + appRow.find("a", {"class": "downloadLink"})["href"]
        return None

    def get_app_details(self, app_download_link):
        time.sleep(self.timeout)
        try:
            resp = self.scraper.get(app_download_link, headers=self.headers)
            resp.raise_for_status()
        except Exception as e:
            logger.error(f"Failed to retrieve app details: {e}")
            return {}
        
        soup = BeautifulSoup(resp.text, "html.parser")
        
        table_rows = soup.find_all("div", {"class": ["table-row", "headerFont"]})
        if len(table_rows) < 2:
            logger.error("Failed to find table rows in app details page")
            return {}
        
        data = table_rows[1]
        
        cells = data.find_all("div", {"class": ["table-cell", "rowheight", "addseparator", "expand", "pad", "dowrap"]})
        if len(cells) < 3:
            logger.error("Failed to find cells in app details table")
            return {}
        
        try:
            architecture = cells[1].text.strip()
            android_version = cells[2].text.strip()
            dpi = cells[3].text.strip()
            download_link = self.base_url + data.find_all("a", {"class": "accent_color"})[0]["href"]
        except IndexError:
            logger.error("Failed to extract app details from cells")
            return {}
        
        return {
            "architecture": architecture,
            "android_version": android_version,
            "dpi": dpi,
            "download_link": download_link,
        }

    def get_download_link(self, app_download_link):
        time.sleep(self.timeout)
        resp = self.scraper.get(app_download_link, headers=self.headers)
        soup = BeautifulSoup(resp.text, "html.parser")
        return self.base_url + str(soup.find_all("a", {"class": "downloadButton"})[0]["href"])

    def get_direct_download_link(self, app_download_url):
        time.sleep(self.timeout)
        resp = self.scraper.get(app_download_url, headers=self.headers)
        soup = BeautifulSoup(resp.text, "html.parser")
        return soup.find("a", {"rel": "nofollow", "data-google-interstitial": "false"})["href"]

# --- thefeed release asset helpers ---
# Release matrix produces three families:
#   thefeed-server-*          -> server binary (skip — clients only)
#   thefeed-client-<plat>-*   -> CLI client binary (linux/darwin/freebsd/
#                                windows + android raw "termux" binary)
#   thefeed-android-*.apk     -> Android app
#   thefeed-ios-*.ipa         -> iOS app

def is_client_asset(name: str) -> bool:
    n = name.lower()
    if n.startswith('thefeed-server'):
        return False
    if n.startswith('thefeed-client'):
        return True
    if n.startswith('thefeed-android') and n.endswith('.apk'):
        return True
    if n.startswith('thefeed-ios') and n.endswith('.ipa'):
        return True
    if n.startswith('thefeed-macos') and n.endswith('.dmg'):
        return True
    return False

# Requested order: openbsd -> termux -> darwin -> linux -> windows -> android
# (termux = thefeed-client-android-* raw Go binary; android = .apk).
# iOS and anything unknown sort to the end. Within a bucket: 64-bit
# variants and the "universal" APK are surfaced first.
def asset_sort_key(name: str):
    n = name.lower()
    # within-bucket tiebreaker — smaller wins, so 64-bit first.
    # 'universal' android APK is pushed to the end of its bucket: when
    # users see four arch-specific APKs first they pick the one that
    # matches their phone; only readers who don't recognise their arch
    # fall through to the catch-all universal APK at the bottom.
    # macOS .dmg is the opposite — it's the drag-install GUI app and
    # should land FIRST inside the darwin bucket, ahead of the raw CLI
    # client binaries which are only useful to command-line users.
    if n.startswith('thefeed-macos') and n.endswith('.dmg'):
        sub = 0
    elif 'universal' in n:
        sub = 9
    elif 'arm64' in n or 'amd64' in n or 'x86_64' in n:
        sub = 1
    else:
        sub = 2
    if 'openbsd' in n or 'freebsd' in n:
        return (0, sub, n)
    if n.startswith('thefeed-client-android'):
        return (1, sub, n)
    # macOS .dmg sits in the darwin bucket alongside the CLI clients.
    if 'darwin' in n or (n.startswith('thefeed-macos') and n.endswith('.dmg')):
        return (2, sub, n)
    if 'linux' in n:
        return (3, sub, n)
    if 'windows' in n:
        return (4, sub, n)
    if n.startswith('thefeed-android') and n.endswith('.apk'):
        return (5, sub, n)
    if 'ios' in n or n.endswith('.ipa'):
        return (6, sub, n)
    return (9, sub, n)

def describe_asset(name: str) -> str:
    """Short Persian one-liner describing a client asset."""
    n = name.lower()
    # Android APKs
    if n.startswith('thefeed-android') and n.endswith('.apk'):
        if 'universal' in n:
            return 'مناسب همه گوشی‌های اندروید (پیشنهادی)'
        if 'arm64-v8a' in n:
            return 'اندروید — گوشی‌های جدید ۶۴ بیتی (سبک‌تر)'
        if 'armeabi-v7a' in n:
            return 'اندروید — گوشی‌های قدیمی ۳۲ بیتی'
        if 'x86_64' in n:
            return 'اندروید — کروم‌بوک، شبیه‌ساز و WSA (۶۴ بیتی)'
        if 'x86' in n:
            return 'اندروید — اینتل ۳۲ بیتی (نادر)'
        return 'نسخه اندروید'
    # Termux / raw android Go binary
    if n.startswith('thefeed-client-android'):
        if 'arm64' in n:
            return 'کلاینت خط فرمان اندروید (Termux، ARM ۶۴ بیتی)'
        if 'arm' in n:
            return 'کلاینت خط فرمان اندروید (Termux، ARM ۳۲ بیتی)'
        return 'کلاینت خط فرمان اندروید (Termux)'
    # Desktop / server-OS clients
    if n.startswith('thefeed-macos') and n.endswith('.dmg'):
        # First-line headline (bolded by caption builder) + multi-line
        # body explaining Gatekeeper first-launch workaround. Unsigned
        # .dmg → macOS shows "cannot verify developer, move to Trash?"
        # the first time; user must Done → Settings → Privacy & Security
        # → Open Anyway.
        return (
            'مک — نصب با کشیدن (یونیورسال: Intel + Apple Silicon، پیشنهادی)'
            '\n\n'
            '⚠️ بار اول که برنامه رو اجرا می‌کنید، macOS می‌گه '
            '«این برنامه از طرف اپل تأیید نشده، آیا می‌خواهید پاکش کنم؟ '
            'امن نیست!». روی Done (یا Cancel) بزنید — هرگز Move to Trash نزنید. '
            'بعد برید به System Settings ← Privacy & Security، '
            'تا پایین اسکرول کنید، اونجا یه پیام در مورد thefeed '
            'و دکمه‌ی Open Anyway می‌بینید. روی Open Anyway بزنید '
            'و در پنجره‌ی بعدی هم Open رو تأیید کنید. '
            'از این به بعد بدون مشکل اجرا میشه.'
        )
    if 'darwin' in n:
        if 'arm64' in n:
            return 'کلاینت خط فرمان مک (Apple Silicon — M1/M2/M3)'
        if 'amd64' in n:
            return 'کلاینت خط فرمان مک (Intel)'
        return 'کلاینت خط فرمان مک'
    if 'linux' in n:
        if 'arm64' in n:
            return 'کلاینت لینوکس ARM ۶۴ بیتی (مثل رزبری‌پای)'
        if 'amd64' in n:
            return 'کلاینت لینوکس ۶۴ بیتی (Intel/AMD)'
        return 'کلاینت لینوکس'
    if 'freebsd' in n:
        if 'arm64' in n:
            return 'کلاینت FreeBSD ARM ۶۴ بیتی'
        if 'amd64' in n:
            return 'کلاینت FreeBSD ۶۴ بیتی'
        return 'کلاینت FreeBSD'
    if 'openbsd' in n:
        return 'کلاینت OpenBSD'
    if 'windows' in n:
        # Order matters: '386' and 'arm64' must be checked before the plain
        # 64-bit fallback, or every Windows build is labelled "۶۴ بیتی".
        if 'arm64' in n:
            return 'کلاینت ویندوز ARM ۶۴ بیتی (Surface / Snapdragon)'
        if '386' in n:
            return 'کلاینت ویندوز ۳۲ بیتی (سیستم‌های قدیمی)'
        if 'amd64' in n or 'x86_64' in n:
            return 'کلاینت ویندوز ۶۴ بیتی (پیشنهادی)'
        return 'کلاینت ویندوز'
    if n.endswith('.ipa') or 'ios' in n:
        return 'iOS — نسخه پیش‌نمایش بدون امضا (نیاز به sideload)'
    return ''


class GitHubReleaseBot:
    def __init__(self):
        self.config = Config()
        self.processed_releases = {}
        # (persian name, filename, t.me link) for every file uploaded this run,
        # replayed as a quoted block in the summary so a release post can link
        # straight to each binary in the channel.
        self.uploaded_links = []
        self._link_base = None
        self.client = None
        self.apkmirror = APKMirror()
        self.last_report_message_id = None
        self.load_config()
        self.load_processed_releases()
    
    def load_config(self):
        """Load configuration from config.json"""
        try:
            with open('config.json', 'r') as f:
                data = json.load(f)
                
            # Load telegram config
            self.config.telegram = data.get('telegram', {})
            
            # Load repositories
            self.config.repositories = []
            for repo_data in data.get('repositories', []):
                repo = Repository()
                repo.name = repo_data.get('name', '')
                repo.github_url = repo_data.get('github_url', '')
                repo.google_play_url = repo_data.get('google_play_url', '')
                repo.apple_store_url = repo_data.get('apple_store_url', '')
                repo.microsoft_store_url = repo_data.get('microsoft_store_url', '')
                repo.testflight_url = repo_data.get('testflight_url', '')
                self.config.repositories.append(repo)
                
            logger.info("Configuration loaded successfully")
            
        except Exception as e:
            logger.error(f"Error loading config: {e}")
            raise
    
    def get_latest_version(self, repo_name: str) -> str:
        """Get latest version for a repository from new array structure"""
        releases = self.processed_releases.get(repo_name, [])
        if isinstance(releases, list) and releases:
            return releases[0].get('version', '')
        elif isinstance(releases, str):
            # Handle old format for backward compatibility
            return releases
        return ''
    
    def add_version(self, repo_name: str, version: str):
        """Add new version to repository with current timestamp, keeping only the latest version"""
        current_time = int(time.time())
        
        # Get existing releases
        releases = self.processed_releases.get(repo_name, [])
        
        # Handle old format
        if isinstance(releases, str):
            releases = [{'version': releases, 'timestamp': current_time}]
        
        # Create new entry
        new_entry = {'version': version, 'timestamp': current_time}
        
        # Keep only the latest version (replace the entire list with just the new version)
        self.processed_releases[repo_name] = [new_entry]
    
    def load_processed_releases(self):
        """Load processed releases from file"""
        try:
            if os.path.exists('processed_releases.json'):
                with open('processed_releases.json', 'r') as f:
                    self.processed_releases = json.load(f)
                # Load last report message ID
                self.last_report_message_id = self.processed_releases.get('lastReportMessageId', None)
                if self.last_report_message_id:
                    logger.info(f"Loaded last report message ID: {self.last_report_message_id}")
            else:
                self.processed_releases = {}
                self.last_report_message_id = None
                logger.info("No existing processed releases file found, starting fresh")
        except Exception as e:
            logger.error(f"Error loading processed releases: {e}")
            self.processed_releases = {}
            self.last_report_message_id = None
    
    def save_processed_releases(self):
        """Save processed releases to file"""
        try:
            import os
            current_dir = os.getcwd()
            logger.info(f"Saving processed releases to {current_dir}/processed_releases.json")
            
            # Save last report message ID
            if self.last_report_message_id:
                self.processed_releases['lastReportMessageId'] = self.last_report_message_id
                logger.info(f"Saving last report message ID: {self.last_report_message_id}")
            
            logger.info(f"Current processed_releases data: {self.processed_releases}")
            
            with open('processed_releases.json', 'w') as f:
                json.dump(self.processed_releases, f, indent=2)
            
            logger.info("Successfully saved processed releases")
            
            # Verify file was written
            if os.path.exists('processed_releases.json'):
                file_size = os.path.getsize('processed_releases.json')
                logger.info(f"processed_releases.json exists, size: {file_size} bytes")
            else:
                logger.error("processed_releases.json was not created!")
                
        except Exception as e:
            logger.error(f"Error saving processed releases: {e}")
            import traceback
            logger.error(f"Traceback: {traceback.format_exc()}")
    
    def get_file_hash(self, content: bytes) -> str:
        """Calculate SHA256 hash of file content"""
        return hashlib.sha256(content).hexdigest()
    
    def extract_package_name(self, url: str) -> str:
        """Extract package name from Google Play URL"""
        from urllib.parse import urlparse, parse_qs
        parsed = urlparse(url)
        query = parse_qs(parsed.query)
        return query.get('id', [''])[0]
    
    def is_newer_version(self, new_tag: str, old_tag: str) -> bool:
        if not old_tag:
            return True
        try:
            return Version(new_tag) > Version(old_tag)
        except:
            return new_tag != old_tag
    
    def create_caption(self, repo: Repository, release: dict, file_hashes: Dict[str, str]) -> str:
        """Create caption for release"""
        caption = f"🚀 ریلیز جدید: {repo.name}\\n\\n"
        caption += f"📦 نسخه: {release.get('tag_name', 'N/A')}\\n"
        caption += f"📅 تاریخ: {release.get('published_at', 'N/A')}\\n\\n"
        
        if repo.github_url:
            caption += f"🔗 Github: {repo.github_url}\\n"
        if repo.google_play_url:
            caption += f"🤖 Google Play: {repo.google_play_url}\\n"
        if repo.apple_store_url:
            caption += f"💰 App Store: {repo.apple_store_url}\\n"
        if repo.microsoft_store_url:
            caption += f"🪟 Microsoft Store: {repo.microsoft_store_url}\\n"
        
        if file_hashes:
            caption += "\\n🔒 هش‌های SHA256:\\n"
            for filename, hash_value in sorted(file_hashes.items()):
                caption += f"📎 {filename}:\\n`{hash_value}`\\n"
        
        return caption
    
    async def send_release_to_channel(self, repo: Repository, release: dict):
        """Send release to channel"""
        import os
        
        # Get channel info
        channel_id = self.config.telegram.get('channel_id')
        channel_username = self.config.telegram.get('channel_username', '').lstrip('@')
        
        if not channel_id:
            logger.error("No channel ID configured")
            return
        
        try:
            channel_id = int(channel_id)
        except ValueError:
            logger.error("Channel ID must be numeric")
            return
        
        # Send introduction message
        if repo.github_url:
            intro_caption = f"🚀 New Release: #{repo.name}\n\n📦 Version: {release.get('tag_name', 'N/A')}\n🏷️ Type: {'Pre-release' if release.get('prerelease', False) else 'Stable'}\n📅 Date: {release.get('published_at', 'N/A')}\n\n⚓️ Github: {repo.github_url}\n🔗 {repo.github_url}/releases"
        elif repo.google_play_url:
            intro_caption = f"🚀 New Release: #{repo.name}\n\n📦 Version: {release.get('tag_name', 'N/A')}\n📅 Date: {release.get('published_at', 'N/A')}\n\n🤖 Google Play: {repo.google_play_url}"
        else:
            intro_caption = f"🚀 New Release: #{repo.name}\n\n📦 Version: {release.get('tag_name', 'N/A')}\n📅 Date: {release.get('published_at', 'N/A')}"

        # Intro has no buttons — per-file messages carry the download link.
        channel_url = f"https://t.me/{channel_username}" if channel_username else f"https://t.me/c/{abs(channel_id)}"
        keyboard = None
        
        # Fetch README.md if this is a GitHub repository
        readme_file_path = None
        if repo.github_url:
            readme_content = await self.get_github_readme(repo.github_url)
            if readme_content:
                try:
                    # Create README.md file with proper name
                    import tempfile
                    import os
                    temp_dir = tempfile.gettempdir()
                    readme_file_path = os.path.join(temp_dir, 'README.md')
                    
                    with open(readme_file_path, 'w', encoding='utf-8') as readme_file:
                        readme_file.write(readme_content)
                    
                    logger.info(f"Successfully fetched README.md for {repo.name}")
                except Exception as e:
                    logger.error(f"Error creating README.md file for {repo.name}: {e}")
                    readme_file_path = None
            else:
                logger.info(f"No README.md found for {repo.name}")
        
        # Send introduction message with README.md attached if available
        if readme_file_path:
            # Send message with README.md as attached document (caption on top)
            await self.client.send_file(
                channel_id,
                file=readme_file_path,
                caption=intro_caption,
                buttons=keyboard,
                parse_mode='md',
                force_document=False  # This allows caption to be on top
            )
            logger.info("Successfully sent introduction message with README.md attached")
            
            # Clean up temp file
            os.unlink(readme_file_path)
            
            # Delay after sending message with attachment
            await asyncio.sleep(5)
        else:
            # Send regular text message without README
            await self.client.send_message(
                channel_id,
                intro_caption,
                buttons=keyboard
            )
            logger.info("Successfully sent introduction message")
            
            # Delay to avoid rate limits
            await asyncio.sleep(5)
        
        # Process assets
        assets = release.get('assets', [])
        if not assets:
            logger.info("No assets found in release")
            return

        # Keep client assets only (drop thefeed-server-*, checksums, notes).
        # Server binaries are not for end-users in this channel.
        assets = [a for a in assets if is_client_asset(a.get('name', ''))]

        # Order: openbsd -> termux -> darwin -> linux -> windows -> android.
        assets.sort(key=lambda a: asset_sort_key(a.get('name', '')))

        logger.info(f"Found {len(assets)} client assets in release (after filter+sort)")

        # Process each asset individually
        for asset in assets:
            asset_name = asset.get('name', 'unknown')
            download_url = asset.get('browser_download_url', '')

            if not download_url:
                logger.error(f"No download URL for asset: {asset_name}")
                continue

            # Skip files with unwanted extensions
            skipped_extensions = {'.sha256', '.txt', '.yml', '.blockmap', '.idsig', '.md'}
            file_extension = os.path.splitext(asset_name)[1].lower()
            if file_extension in skipped_extensions:
                logger.info(f"Skipping asset {asset_name} (unwanted extension: {file_extension})")
                continue

            logger.info(f"Processing asset: {asset_name}")
            
            # Download file to temp
            try:
                import requests
                response = requests.get(download_url, stream=True)
                response.raise_for_status()
                
                import tempfile
                import os
                import hashlib
                hash_obj = hashlib.sha256()
                with tempfile.NamedTemporaryFile(delete=False, dir=os.getcwd()) as temp_file:
                    total_size = int(response.headers.get('content-length', 0)) if response.headers.get('content-length') else 0
                    downloaded = 0
                    last_percent = 0
                    logger.info(f"Starting download: {asset_name} (Size: {total_size // (1024*1024)} MB)")
                    for chunk in response.iter_content(chunk_size=8192):
                        temp_file.write(chunk)
                        hash_obj.update(chunk)
                        downloaded += len(chunk)
                        if total_size > 0:
                            percent = (downloaded / total_size) * 100
                            if percent - last_percent >= 5 or percent >= 100:
                                logger.info(f"Downloading {asset_name}: [{'#' * int(percent // 5)}{' ' * (20 - int(percent // 5))}] {percent:.1f}%")
                                last_percent = percent
                    temp_file_path = temp_file.name
                
                file_hash = hash_obj.hexdigest()
                
                # Send file immediately after download
                try:
                    logger.info(f"Attempting to send file: {temp_file_path} as {asset_name}")
                    logger.info(f"File size: {os.path.getsize(temp_file_path)} bytes")
                    logger.info(f"Starting upload to Telegram...")
                    
                    # Create progress callback for upload
                    def upload_progress(current, total):
                        if total > 0:
                            percent = (current / total) * 100
                            if percent % 5 == 0 and percent > 0:  # Log every 5%
                                logger.info(f"Uploading {asset_name}: [{'=' * int(percent // 5)}{' ' * (20 - int(percent // 5))}] {percent:.1f}%")
                    
                    # First upload the file to get a file handle
                    logger.info(f"Uploading file with upload_file method...")
                    uploaded_file = await self.client.upload_file(
                        temp_file_path,
                        file_name=asset_name,
                        progress_callback=upload_progress
                    )
                    logger.info(f"File uploaded successfully: {uploaded_file}")
                    
                    # Only the source-download button. The previous
                    # "Github Mirror" channel button is removed.
                    if repo.github_url:
                        download_text = "📥 Download from Github"
                    elif repo.google_play_url:
                        download_text = "📥 Download from APKMirror"
                    else:
                        download_text = "📥 Download"
                    keyboard = [[Button.url(download_text, url=download_url)]]

                    # Caption — Persian description goes in its own
                    # paragraph at the end. Mixing RTL Persian into an
                    # LTR line shoves the whole line to the right edge
                    # on Telegram clients, so keep it on a separate
                    # paragraph after a blank line. If describe_asset
                    # returns multiple paragraphs (e.g. the DMG with
                    # first-launch Gatekeeper instructions), bold only
                    # the headline and render the rest as regular text.
                    description = describe_asset(asset_name)
                    if description:
                        head, _, rest = description.partition('\n\n')
                        desc_block = f"\n\n**{head}**"
                        if rest:
                            desc_block += f"\n\n{rest}"
                    else:
                        desc_block = ""
                    caption = (
                        f"#{repo.name}\n"
                        f"📦 Version: `{release.get('tag_name', 'N/A')}`\n"
                        f"📎 File: `{asset_name}`\n"
                        f"🔒 SHA256: `{file_hash}`"
                        f"{desc_block}"
                    )

                    # Then send the file using the handle
                    logger.info(f"Sending file with send_file method...")
                    sent_file = await self.client.send_file(
                        channel_id,
                        file=uploaded_file,
                        caption=caption,
                        buttons=keyboard,
                        parse_mode='md'
                    )

                    link = await self.message_link(channel_id, sent_file)
                    if link:
                        self.uploaded_links.append({
                            'repo': repo.name,
                            'version': release.get('tag_name', ''),
                            'name': asset_name,
                            'desc': (description.partition('\n\n')[0] if description else asset_name),
                            'link': link,
                        })

                    logger.info(f"Successfully sent file: {asset_name}")
                    
                    # Add delay between uploads
                    await asyncio.sleep(5)
                    
                    os.unlink(temp_file_path)
                    
                except Exception as e:
                    logger.error(f"Error sending file {asset_name}: {e}", exc_info=True)
                    # Send fallback message with download button only.
                    size_mb = os.path.getsize(temp_file_path) // (1024 * 1024)
                    fallback_msg = f"📎 File: `{asset_name}`\n\n📊 Size: {size_mb} MB\n\n⚠️ Download from {'GitHub' if repo.github_url else 'APKMirror'}:"

                    keyboard = [[Button.url(download_text, url=download_url)]]
                    
                    await self.client.send_message(
                        channel_id,
                        fallback_msg,
                        buttons=keyboard,
                        parse_mode='md'
                    )
                    os.unlink(temp_file_path)
                    
                    # Delay after fallback
                    await asyncio.sleep(5)
                
            except Exception as e:
                logger.error(f"Error downloading {asset_name}: {e}")
                continue
        
        logger.info(f"Successfully sent release {release.get('tag_name', 'unknown')} for {repo.name}")
    
    async def message_link(self, channel_id, message) -> str:
        """Public t.me link to one uploaded file, or '' if it can't be built."""
        mid = getattr(message, 'id', None)
        if not mid:
            return ''
        if self._link_base is None:
            # Resolve from the entity rather than config: the configured
            # channel_username is the announcement channel, not necessarily
            # the one files are uploaded to.
            username = None
            try:
                entity = await self.client.get_entity(channel_id)
                username = getattr(entity, 'username', None)
            except Exception as e:
                logger.warning(f"Could not resolve channel entity for links: {e}")
            if username:
                self._link_base = f"https://t.me/{username}"
            else:
                internal = str(channel_id)
                # -100XXXXXXXXXX is the bot-API form of a channel id; the
                # private t.me/c/ link uses the bare internal id.
                internal = internal[4:] if internal.startswith('-100') else internal.lstrip('-')
                self._link_base = f"https://t.me/c/{internal}"
        return f"{self._link_base}/{mid}"

    async def delete_previous_reports(self):
        """Delete the last report message"""
        channel_id = self.config.telegram.get('channel_id')
        
        if not channel_id:
            logger.error("No channel ID configured for deleting reports")
            return
        
        try:
            channel_id = int(channel_id)
        except ValueError:
            logger.error("Channel ID must be numeric")
            return
        
        try:
            if self.last_report_message_id:
                logger.info(f"Deleting previous report message ID: {self.last_report_message_id}")
                await self.client.delete_messages(channel_id, [self.last_report_message_id])
                logger.info("Successfully deleted previous report message")
                self.last_report_message_id = None
            else:
                logger.info("No previous report message ID found, skipping deletion")
            
        except Exception as e:
            logger.error(f"Error deleting previous report: {e}")
    
    def store_links(self):
        """(label, url) install links per repo, App Store preferred over TestFlight."""
        out = []
        for repo in self.config.repositories:
            if repo.google_play_url:
                out.append(("گوگل پلی", repo.google_play_url))
            if repo.apple_store_url:
                out.append(("اپ استور", repo.apple_store_url))
            elif repo.testflight_url:
                out.append(("تست‌فلایت (iOS)", repo.testflight_url))
        return out

    def build_install_buttons(self):
        """One row of store buttons, or none when no store URL is configured."""
        row = [Button.url(f"⬇️ {label}", url) for label, url in self.store_links()]
        return [row] if row else []

    def build_install_block(self) -> str:
        """Store links as plain text too, so they can be copied into a post."""
        links = self.store_links()
        if not links:
            return ""
        out = "\n📲 نصب مستقیم:\n<blockquote>"
        out += "\n".join(f"{html_escape(label)}\n{html_escape(url)}" for label, url in links)
        return out + "</blockquote>\n"

    def build_links_block(self) -> str:
        """Quoted list of this run's uploads: Persian name + direct t.me link.

        Kept as a blockquote so the report stays compact, and the links are
        plain text so they can be copied straight into a release post. The
        name and the URL sit on separate lines — a Persian label and an LTR
        URL on one line get visually scrambled by bidi reordering.
        """
        if not self.uploaded_links:
            return ""
        out = "\n🔗 لینک مستقیم فایل‌ها:\n<blockquote>"
        current = None
        for i, item in enumerate(self.uploaded_links):
            header = f"{item['repo']} {item['version']}".strip()
            if header != current:
                current = header
                prefix = "" if i == 0 else "\n"
                out += f"{prefix}<b>{html_escape(header)}</b>\n"
            out += f"{html_escape(item['desc'])}\n{html_escape(item['link'])}\n"
        return out.rstrip("\n") + "</blockquote>\n"

    async def send_summary_message(self):
        """Send summary message with list of supported programs"""
        # Get channel info
        channel_id = self.config.telegram.get('channel_id')
        channel_username = self.config.telegram.get('channel_username', '').lstrip('@')
        
        if not channel_id:
            logger.error("No channel ID configured for summary message")
            return
        
        try:
            channel_id = int(channel_id)
        except ValueError:
            logger.error("Channel ID must be numeric")
            return
        
        # HTML, not markdown: the per-file link list below needs a real
        # <blockquote>, which markdown has no syntax for.
        message_text = "#گزارش\n"
        message_text += "وضعیت آخرین بروزرسانی برنامه‌ها مورد بررسی قرار گرفت.\n\n"
        message_text += "📦 پروژه‌های پشتیبانی شده:\n"
        
        # Create list of repositories with their latest info for sorting
        repo_info = []
        for repo in self.config.repositories:
            releases = self.processed_releases.get(repo.name, [])
            if isinstance(releases, list) and releases:
                latest_entry = releases[0]
                repo_info.append({
                    'name': repo.name,
                    'version': latest_entry.get('version', 'نامشخص'),
                    'timestamp': latest_entry.get('timestamp', 0)
                })
            elif isinstance(releases, str):
                # Handle old format
                repo_info.append({
                    'name': repo.name,
                    'version': releases,
                    'timestamp': 0  # Old entries get timestamp 0, will appear first
                })
            else:
                repo_info.append({
                    'name': repo.name,
                    'version': 'نامشخص',
                    'timestamp': 0
                })
        
        # Sort by timestamp (oldest first, newest last)
        repo_info.sort(key=lambda x: x['timestamp'])
        
        # Add sorted repositories to message
        for info in repo_info:
            message_text += f"#{html_escape(info['name'])}: <code>{html_escape(info['version'])}</code>\n"

        message_text += self.build_install_block()
        message_text += self.build_links_block()

        # Store buttons first — they are the easiest install path — then the
        # thefeed channel directory.
        keyboard = self.build_install_buttons() + [
            [Button.url("📢 کانال اصلی دفید", "https://t.me/networkti")],
            [Button.url("📦 کانال فایل‌های باینری/نصبی دفید", "https://t.me/thefeedfile")],
            [Button.url("⚙ کانال کانفیگ‌های دفید", "https://t.me/thefeedconfig")],
        ]
        
        try:
            sent_message = await self.client.send_message(
                channel_id,
                message_text,
                buttons=keyboard,
                parse_mode='html',
                link_preview=False
            )
            logger.info("Summary message sent successfully")
            
            # Save the message ID for future deletion
            if sent_message and hasattr(sent_message, 'id'):
                self.last_report_message_id = sent_message.id
                logger.info(f"Saved report message ID: {self.last_report_message_id}")
                # Save to file immediately
                self.save_processed_releases()
            
            # Small delay after summary
            await asyncio.sleep(3)
            
        except Exception as e:
            logger.error(f"Error sending summary message: {e}")
    
    async def check_all_repositories(self):
        """Check all repositories for new releases"""
        logger.info("Checking all repositories for new releases")
        
        had_new_releases = False
        
        for repo in self.config.repositories:
            logger.info(f"Checking repository: {repo.name}")
            
            try:
                latest_release = None
                if repo.github_url:
                    releases = await self.get_github_releases(repo.github_url)
                    
                    if not releases:
                        logger.info(f"No releases found for {repo.name}")
                        continue
                    
                    # Get latest non-draft, non-RC release. Release
                    # candidates (tags like vX.Y.Z-rc1, -rc.2, -RC3)
                    # are skipped — only stable tags get published to
                    # the channel.
                    for release in releases:
                        if release.get('draft', False):
                            continue
                        tag = release.get('tag_name', '')
                        if '-rc' in tag.lower():
                            logger.info(
                                f"Skipping RC release {tag} for {repo.name}"
                            )
                            continue
                        latest_release = release
                        break

                    if not latest_release:
                        logger.info(f"No stable release found for {repo.name}")
                        continue
                elif repo.google_play_url:
                    latest_release = await self.get_apkmirror_release(repo)
                    if not latest_release:
                        logger.info(f"No release found for {repo.name}")
                        continue
                else:
                    logger.info(f"No GitHub or Google Play URL for {repo.name}")
                    continue
                
                tag = latest_release.get('tag_name', '')
                stored_tag = self.get_latest_version(repo.name)
                if self.is_newer_version(tag, stored_tag):
                    logger.info(f"Latest release for {repo.name}: {tag}")
                    await self.send_release_to_channel(repo, latest_release)
                    self.add_version(repo.name, tag)
                    self.save_processed_releases()
                    had_new_releases = True
                else:
                    logger.info(f"No new release for {repo.name}, latest is {tag}, stored is {stored_tag}")
                
            except Exception as e:
                logger.error(f"Error checking {repo.name}: {e}")
                continue
        
        return had_new_releases
    
    async def get_github_releases(self, github_url: str) -> List[dict]:
        """Get releases from GitHub API"""
        try:
            import requests
            
            # Extract owner and repo from URL
            parts = github_url.strip('/').split('/')
            if len(parts) < 5:
                raise ValueError(f"Invalid GitHub URL: {github_url}")
            
            owner = parts[3]
            repo_name = parts[4]
            
            api_url = f"https://api.github.com/repos/{owner}/{repo_name}/releases"
            
            headers = {
                'User-Agent': 'GitHub-Release-Bot/1.0',
                'Accept': 'application/vnd.github.v3+json'
            }
            
            for attempt in range(3):
                try:
                    response = requests.get(api_url, headers=headers, timeout=30)
                    response.raise_for_status()
                    releases = response.json()
                    return releases
                except requests.exceptions.RequestException as e:
                    logger.warning(f"Attempt {attempt + 1} failed: {e}")
                    if attempt < 2:
                        time.sleep(5)
            
            # If all attempts failed
            raise Exception("Failed to fetch releases after 3 attempts")
            
        except Exception as e:
            logger.error(f"Error fetching releases: {e}")
            return []
    
    async def get_github_readme(self, github_url: str) -> Optional[str]:
        """Fetch README.md content from GitHub repository"""
        try:
            import requests
            
            # Extract owner and repo from URL
            parts = github_url.strip('/').split('/')
            if len(parts) < 5:
                logger.error(f"Invalid GitHub URL: {github_url}")
                return None
            
            owner = parts[3]
            repo_name = parts[4]
            
            # Try different README filenames
            readme_names = ['README.md', 'readme.md', 'README.markdown', 'README']
            
            headers = {
                'User-Agent': 'GitHub-Release-Bot/1.0',
                'Accept': 'application/vnd.github.v3+json'
            }
            
            for readme_name in readme_names:
                api_url = f"https://api.github.com/repos/{owner}/{repo_name}/contents/{readme_name}"
                
                try:
                    response = requests.get(api_url, headers=headers, timeout=30)
                    if response.status_code == 200:
                        data = response.json()
                        if data.get('content') and data.get('encoding') == 'base64':
                            import base64
                            content = base64.b64decode(data['content']).decode('utf-8')
                            logger.info(f"Successfully fetched {readme_name} from {owner}/{repo_name}")
                            return content
                    elif response.status_code == 404:
                        continue  # Try next README name
                    else:
                        response.raise_for_status()
                        
                except requests.exceptions.RequestException as e:
                    logger.warning(f"Failed to fetch {readme_name}: {e}")
                    continue
            
            logger.info(f"No README file found in {owner}/{repo_name}")
            return None
            
        except Exception as e:
            logger.error(f"Error fetching README: {e}")
            return None
    
    async def get_apkmirror_release(self, repo: Repository) -> dict:
        """Get latest release from APKMirror"""
        try:
            package_name = self.extract_package_name(repo.google_play_url)
            if not package_name:
                logger.error(f"Could not extract package name from {repo.google_play_url}")
                return {}
            
            # Search for the app
            apps = self.apkmirror.search(package_name)
            if not apps and repo.name:
                logger.info(f"No apps found for package {package_name}, trying name {repo.name}")
                apps = self.apkmirror.search(repo.name)
            
            if not apps:
                logger.error(f"No apps found for {package_name} or {repo.name}")
                return {}
            
            app_link = apps[0]['link']  # Take the first result
            
            # Get latest version link
            version_link = self.apkmirror.get_latest_version_link(app_link)
            if not version_link:
                logger.error(f"No version link found for {repo.name}")
                return {}
            
            # Extract version from link
            version = version_link.split('/')[-1].replace('-release', '').replace('-', '.')
            
            # Get download link for universal APK
            details = self.apkmirror.get_app_details(version_link)
            if not details:
                logger.error(f"Failed to get app details for {repo.name}")
                return {}
            
            if details.get('architecture') != 'universal':
                logger.warning(f"No universal APK found for {repo.name}, architecture: {details.get('architecture')}")
                # For now, return empty, but perhaps download anyway
                return {}
            
            download_link = self.apkmirror.get_download_link(details['download_link'])
            direct_link = self.apkmirror.get_direct_download_link(download_link)
            
            # Create release dict
            release = {
                'tag_name': version,
                'published_at': datetime.now().isoformat(),
                'assets': [{
                    'name': f'{package_name}.apk',
                    'browser_download_url': direct_link
                }]
            }
            return release
            
        except Exception as e:
            logger.error(f"Error fetching APKMirror release for {repo.name}: {e}")
            return {}
    
    async def run(self):
        """Run the bot"""
        # Load environment variables from .env file first
        env_vars = load_env()
        
        # Get credentials from environment variables or .env file
        api_id = int(env_vars.get('TELEGRAM_API_ID', os.getenv('TELEGRAM_API_ID', '0')))
        api_hash = env_vars.get('TELEGRAM_API_HASH', os.getenv('TELEGRAM_API_HASH', ''))
        bot_token = env_vars.get('TELEGRAM_BOT_TOKEN', os.getenv('TELEGRAM_BOT_TOKEN', ''))
        
        if not all([api_id, api_hash, bot_token]):
            logger.error("Missing required environment variables")
            logger.info("Please set TELEGRAM_API_ID, TELEGRAM_API_HASH, and TELEGRAM_BOT_TOKEN")
            return
        
        # Create client
        self.client = TelegramClient('bot_session', api_id, api_hash)
        
        try:
            await self.client.start(bot_token=bot_token)
            logger.info("Bot started successfully")
            
            # Run immediately
            had_new_releases = await self.check_all_repositories()
            logger.info("All repositories checked successfully - Bot execution completed")
            
            # Send summary message only if there were new releases
            if had_new_releases:
                # Delete all previous report messages before sending new one
                await self.delete_previous_reports()
                await self.send_summary_message()
            else:
                logger.info("No new releases found, skipping summary message")
            
            logger.info("Exiting gracefully...")
            
        except Exception as e:
            logger.error(f"Error running bot: {e}")
            raise  # Re-raise to ensure non-zero exit code on error
        finally:
            await self.client.disconnect()
            logger.info("Bot disconnected and shut down")

if __name__ == "__main__":
    bot = GitHubReleaseBot()
    asyncio.run(bot.run())
