#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Добавление non-Steam игр в Steam shortcuts.vdf с загрузкой арт-ресурсов из SteamGridDB,
поддержкой безопасного хранения API ключа (keyring / env), устойчивыми HTTP вызовами,
атомарной записью VDF с бэкапом, и проверкой/перезапуском Steam (Windows / Linux native / Flatpak).
"""
from __future__ import annotations

import logging
import os
import shutil
import sys
import tempfile
import time
import zlib
import subprocess
import platform
from pathlib import Path
from typing import Dict, Optional, Tuple

# optional keyring
try:
    import keyring  # type: ignore
    KEYRING_AVAILABLE = True
except Exception:
    KEYRING_AVAILABLE = False

import requests
import vdf
from PIL import Image
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ---------- Конфигурация ----------
SERVICE_NAME = "steamgriddb"
KEYRING_USERNAME = "default"
ENV_API_KEY = "STEAMGRIDDB_API_KEY"
HTTP_TIMEOUT = (5, 20)              # (connect, read) seconds
REQUEST_RETRIES = 3
BACKUP_DIRNAME = "backups_shortcuts"
# ------------------------------------

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("nonsteam-adder")


def default_steam_userdata_path() -> Path:
    """Возвращает путь к userdata в зависимости от платформы."""
    if sys.platform.startswith("win"):
        return Path(r"C:\Program Files (x86)\Steam\userdata")
    if sys.platform.startswith("linux"):
        return Path(os.path.expanduser("~/.steam/steam/userdata"))
    if sys.platform.startswith("darwin"):
        return Path(os.path.expanduser("~/Library/Application Support/Steam/userdata"))
    raise RuntimeError("Unsupported platform")


def requests_session_with_retries() -> requests.Session:
    """Requests.Session с политикой ретраев для устойчивости сетевых операций."""
    s = requests.Session()
    retries = Retry(total=REQUEST_RETRIES, backoff_factor=0.5, status_forcelist=(429, 500, 502, 503, 504))
    adapter = HTTPAdapter(max_retries=retries)
    s.mount("http://", adapter)
    s.mount("https://", adapter)
    return s


def get_api_key(interactive_save: bool = True) -> Optional[str]:
    """
    Получение SteamGridDB API key в порядке приоритета:
      1) системный keyring (если доступен)
      2) переменная окружения STEAMGRIDDB_API_KEY
      3) интерактивный ввод (опционально) с попыткой сохранить в keyring
    """
    if KEYRING_AVAILABLE:
        try:
            k = keyring.get_password(SERVICE_NAME, KEYRING_USERNAME)
            if k:
                logger.info("API key прочитан из системного keyring.")
                return k
        except Exception as e:
            logger.debug("keyring.get_password failed: %s", e)

    env = os.environ.get(ENV_API_KEY)
    if env:
        logger.info("API key прочитан из переменной окружения %s.", ENV_API_KEY)
        return env

    if interactive_save and sys.stdin.isatty():
        try:
            entered = input("Введите SteamGridDB API key (или Enter чтобы пропустить): ").strip()
            if entered:
                if KEYRING_AVAILABLE:
                    try:
                        keyring.set_password(SERVICE_NAME, KEYRING_USERNAME, entered)
                        logger.info("API key сохранён в системный keyring.")
                    except Exception as e:
                        logger.warning("Не удалось сохранить ключ в keyring: %s", e)
                else:
                    logger.info("Keyring недоступен; ключ не сохранён.")
                return entered
        except Exception:
            pass

    logger.warning("API key не найден; вызовы к SteamGridDB будут пропущены.")
    return None


def generate_appid(game_name: str, exe_path: str) -> str:
    """Генерация уникального appid: crc32(exe+name) | 0x80000000 (как в оригинале)."""
    unique = (exe_path + game_name).encode("utf-8")
    legacy_id = zlib.crc32(unique) | 0x80000000
    return str(legacy_id)


def atomic_write_file_with_vdf(path: Path, write_callable):
    """
    Атомарная запись: создаём бэкап, пишем во временный файл в той же директории,
    затем replace. write_callable(fp) — функция, принимающая открытый бинарный fp и пишущая в него vdf.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        backup_dir = path.parent / BACKUP_DIRNAME
        backup_dir.mkdir(parents=True, exist_ok=True)
        timestamp = time.strftime("%Y%m%d-%H%M%S")
        backup_path = backup_dir / f"{path.name}.bak.{timestamp}"
        try:
            shutil.copy2(path, backup_path)
            logger.info("Создан бэкап: %s", backup_path)
        except Exception as e:
            logger.warning("Не удалось создать бэкап: %s", e)

    # пишем во временный файл в той же папке
    with tempfile.NamedTemporaryFile(dir=str(path.parent), delete=False) as tf:
        tmp_path = Path(tf.name)
    try:
        with tmp_path.open("wb") as f:
            write_callable(f)
    except Exception as e:
        # очистить временный файл при ошибке
        try:
            tmp_path.unlink(missing_ok=True)
        except Exception:
            pass
        raise
    tmp_path.replace(path)
    logger.debug("Атомарная запись выполнена: %s", path)


def load_shortcuts_binary(path: Path) -> Dict:
    """Загружает бинарный shortcuts.vdf или возвращает новую структуру при отсутствии."""
    if not path.exists():
        logger.info("shortcuts.vdf не найден, создаётся новая структура.")
        return {"shortcuts": {}}
    try:
        with path.open("rb") as f:
            data = vdf.binary_load(f)
        if "shortcuts" not in data:
            data.setdefault("shortcuts", {})
        return data
    except Exception as e:
        logger.error("Не удалось загрузить бинарный VDF %s: %s", path, e)
        raise


def dump_shortcuts_binary(path: Path, shortcuts_obj: Dict):
    """
    Корректная запись бинарного VDF. Учитываем возможные варианты сигнатуры
    vdf.binary_dump(fp, obj) или vdf.binary_dump(obj, fp).
    Производим атомарную запись через atomic_write_file_with_vdf.
    """
    def writer(fp):
        # пытаемся вызвать наиболее вероятную сигнатуру: binary_dump(fp, obj)
        try:
            vdf.binary_dump(fp, shortcuts_obj)
            return
        except TypeError as e:
            logger.debug("vdf.binary_dump(fp, obj) TypeError: %s — пробуем обратный порядок", e)
        except Exception as e:
            logger.debug("vdf.binary_dump(fp, obj) failed: %s", e)

        # fallback: binary_dump(obj, fp)
        try:
            vdf.binary_dump(shortcuts_obj, fp)
            return
        except Exception as e:
            logger.error("vdf.binary_dump failed с обеих сигнатур: %s", e)
            raise

    atomic_write_file_with_vdf(path, writer)
    logger.info("shortcuts.vdf записан: %s", path)


# ---- Steam detection and control utilities ----

def detect_steam_variant() -> str:
    """
    Определяет вариант Steam:
       'windows', 'linux_flatpak', 'linux_native', 'darwin_native', 'unknown'
    """
    system = platform.system().lower()
    if system.startswith("windows"):
        return "windows"
    if system.startswith("linux"):
        # сначала проверим flatpak
        if shutil.which("flatpak"):
            try:
                r = subprocess.run(["flatpak", "info", "com.valvesoftware.Steam"],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
                if r.returncode == 0:
                    return "linux_flatpak"
            except Exception:
                pass
        # затем проверим наличие команды steam
        if shutil.which("steam"):
            return "linux_native"
        # стандартные места
        possible = [
            Path.home() / ".local" / "share" / "Steam" / "steam.sh",
            Path("/usr/bin/steam"),
            Path("/usr/games/steam")
        ]
        for p in possible:
            if p.exists():
                return "linux_native"
    if system.startswith("darwin"):
        return "darwin_native"
    return "unknown"


def is_steam_running(variant: str) -> bool:
    """Проверяет наличие процессов Steam в системе."""
    try:
        if variant == "windows":
            out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq steam.exe"], capture_output=True, text=True)
            return "steam.exe" in out.stdout.lower()
        if variant == "linux_flatpak":
            out = subprocess.run(["pgrep", "-f", "com.valvesoftware.Steam"], capture_output=True)
            return out.returncode == 0
        # linux_native и darwin_native
        out = subprocess.run(["pgrep", "-f", "steam"], capture_output=True)
        return out.returncode == 0
    except Exception:
        return False


def stop_steam(variant: str) -> bool:
    """Останавливает процессы Steam; возвращает True если команда выполнена (не гарантирует остановку всех процессов)."""
    try:
        if variant == "windows":
            subprocess.run(["taskkill", "/IM", "steam.exe", "/F"], check=False)
            return True
        if variant == "linux_flatpak":
            subprocess.run(["flatpak", "kill", "com.valvesoftware.Steam"], check=False)
            subprocess.run(["pkill", "-f", "steam"], check=False)
            return True
        if variant in ("linux_native", "darwin_native"):
            subprocess.run(["pkill", "-f", "steam"], check=False)
            return True
    except Exception:
        return False
    return False


def start_steam(variant: str) -> bool:
    """Запускает Steam в зависимости от варианта; возвращает True если команда запущена."""
    try:
        if variant == "windows":
            candidates = [
                Path(r"C:\Program Files (x86)\Steam\Steam.exe"),
                Path(r"C:\Program Files\Steam\Steam.exe"),
                Path(r"C:\Program Files (x86)\Steam\steam.exe"),
            ]
            for p in candidates:
                if p.exists():
                    subprocess.Popen([str(p)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    return True
            # fallback: try shell start
            try:
                subprocess.Popen(["start", "steam://open"], shell=True)
                return True
            except Exception:
                return False
        if variant == "linux_flatpak":
            subprocess.Popen(["flatpak", "run", "com.valvesoftware.Steam"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return True
        if variant == "linux_native":
            steam_cmd = shutil.which("steam") or shutil.which("steamcmd")
            if steam_cmd:
                subprocess.Popen([steam_cmd], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                return True
            local_sh = Path.home() / ".local" / "share" / "Steam" / "steam.sh"
            if local_sh.exists():
                subprocess.Popen([str(local_sh)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                return True
        if variant == "darwin_native":
            # macos: попытка открыть приложение через open
            subprocess.Popen(["open", "-a", "Steam"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return True
    except Exception:
        return False
    return False


def restart_steam_if_running(prompt_before_restart: bool = False, allow_restart: bool = True) -> Tuple[str, bool]:
    """
    Определяет вариант Steam, проверяет запущен ли клиент, и если разрешено — останавливает и запускает обратно.
    Возвращает (variant, performed_restart_flag).
    prompt_before_restart: если True и stdin доступен — запросит подтверждение.
    """
    variant = detect_steam_variant()
    running = is_steam_running(variant)
    if not running:
        return variant, False
    if not allow_restart:
        return variant, False
    if prompt_before_restart and sys.stdin.isatty():
        ans = input(f"Steam ({variant}) работает. Остановить и перезапустить? [y/N]: ").strip().lower()
        if ans not in ("y", "yes"):
            logger.info("Пользователь отменил перезапуск Steam.")
            return variant, False
    logger.info("Останавливаем Steam (%s)...", variant)
    stopped = stop_steam(variant)
    time.sleep(1.5)
    logger.info("Запускаем Steam (%s)...", variant)
    started = start_steam(variant)
    performed = stopped or started
    return variant, performed


# ---- Core class ----

class NonSteamGameAdder:
    def __init__(self, steam_dir: Optional[Path] = None, api_key: Optional[str] = None):
        self.steam_dir = steam_dir or default_steam_userdata_path()
        self.api_key = api_key or get_api_key()
        self.session = requests_session_with_retries()

    def _user_path(self, user_id: str) -> Path:
        return self.steam_dir / user_id

    def get_local_steam_usernames(self) -> Dict[str, str]:
        """Сканирует userdata и возвращает {user_id: persona_name|Unknown}."""
        out: Dict[str, str] = {}
        if not self.steam_dir.exists():
            logger.warning("Steam userdata path не найден: %s", self.steam_dir)
            return out
        for d in self.steam_dir.iterdir():
            if not d.is_dir():
                continue
            uid = d.name
            lc = d / "config" / "localconfig.vdf"
            if lc.exists():
                try:
                    with lc.open("r", encoding="utf-8", errors="replace") as f:
                        data = vdf.load(f)
                        username = data.get("UserLocalConfigStore", {}).get("friends", {}).get("PersonaName", "Unknown")
                        out[uid] = username
                except Exception as e:
                    logger.debug("Парсинг %s failed: %s", lc, e)
                    out[uid] = "Unknown"
            else:
                out[uid] = "Unknown"
        return out

    def get_current_steam_user(self) -> Optional[Tuple[str, Dict]]:
        loginusers = (self.steam_dir.parent / "config" / "loginusers.vdf")
        if not loginusers.exists():
            return None
        try:
            with loginusers.open("r", encoding="utf-8", errors="replace") as f:
                data = vdf.load(f)
            users = data.get("users", {})
            for uid, info in users.items():
                if str(info.get("MostRecent", "")) in ("1", "true", "True"):
                    return uid, info
        except Exception as e:
            logger.debug("Failed to parse loginusers.vdf: %s", e)
        return None

    def fetch_steamgriddb_image_url(self, game_id: int, image_type: str) -> Optional[str]:
        """Запрашивает SteamGridDB API и возвращает URL первой подходящей картинки (или None)."""
        if not self.api_key:
            return None
        headers = {"Authorization": f"Bearer {self.api_key}"}
        if image_type == "hero":
            url = f"https://www.steamgriddb.com/api/v2/heroes/game/{game_id}"
        elif image_type == "icon":
            url = f"https://www.steamgriddb.com/api/v2/icons/game/{game_id}"
        elif image_type == "wide_grid":
            url = f"https://www.steamgriddb.com/api/v2/grids/game/{game_id}?dimensions=920x430"
        else:
            url = f"https://www.steamgriddb.com/api/v2/{image_type}s/game/{game_id}"
        try:
            r = self.session.get(url, headers=headers, timeout=HTTP_TIMEOUT)
            logger.info("Fetching %s for %s -> HTTP %s", image_type, game_id, r.status_code)
            if r.status_code == 200:
                payload = r.json()
                if payload.get("success") and payload.get("data"):
                    return payload["data"][0].get("url")
        except Exception as e:
            logger.debug("fetch_steamgriddb_image_url exception: %s", e)
        return None

    def download_image(self, url: str, out_path: Path, resize_to: Optional[Tuple[int, int]] = None) -> bool:
        """Скачивание изображения и опциональный ресайз через PIL."""
        try:
            r = self.session.get(url, timeout=HTTP_TIMEOUT)
            if r.status_code != 200:
                logger.debug("Image download failed %s -> %s", url, r.status_code)
                return False
            out_path.parent.mkdir(parents=True, exist_ok=True)
            with out_path.open("wb") as f:
                f.write(r.content)
            logger.info("Downloaded image %s", out_path)
            if resize_to:
                try:
                    with Image.open(out_path) as img:
                        img = img.convert("RGBA").resize(resize_to, Image.Resampling.LANCZOS)
                        img.save(out_path, format="PNG")
                    logger.info("Resized %s to %s", out_path, resize_to)
                except Exception as e:
                    logger.warning("Failed to resize %s: %s", out_path, e)
            return True
        except Exception as e:
            logger.debug("download_image exception: %s", e)
            return False

    def save_images_to_grid(self, app_id: str, game_id: int, user_id: str):
        """Сохранение набора изображений в userdata/<user>/config/grid."""
        grid_folder = self._user_path(user_id) / "config" / "grid"
        grid_folder.mkdir(parents=True, exist_ok=True)
        types = ["grid", "wide_grid", "hero", "logo", "icon"]
        for t in types:
            url = self.fetch_steamgriddb_image_url(game_id, t)
            if not url:
                continue
            ext = Path(url).suffix or ".png"
            if t == "grid":
                out = grid_folder / f"{app_id}p{ext}"
            elif t == "wide_grid":
                out = grid_folder / f"{app_id}{ext}"
            else:
                out = grid_folder / f"{app_id}_{t}{ext}"
            if t == "icon":
                self.download_image(url, out, resize_to=(64, 64))
            else:
                self.download_image(url, out)

    def search_game_on_steamgriddb(self, game_name: str) -> Optional[int]:
        """Autocomplete поиск на SteamGridDB; возвращает game_id первого результата."""
        if not self.api_key:
            return None
        url = f"https://www.steamgriddb.com/api/v2/search/autocomplete/{requests.utils.requote_uri(game_name)}"
        headers = {"Authorization": f"Bearer {self.api_key}"}
        try:
            r = self.session.get(url, headers=headers, timeout=HTTP_TIMEOUT)
            if r.status_code == 200:
                payload = r.json()
                if payload.get("success") and payload.get("data"):
                    return int(payload["data"][0]["id"])
        except Exception as e:
            logger.debug("search_game_on_steamgriddb exception: %s", e)
        return None

    def add_non_steam_game(self, game_exe_path: str, game_name: str, user_id: str, launch_options: str = "") -> Dict:
        """
        Основной метод: генерирует appid, пытается скачать изображения, и добавляет запись в shortcuts.vdf.
        Перед записью выполняет проверку/перезапуск Steam, чтобы избежать блокировок файла.
        """
        exe = str(Path(game_exe_path).expanduser())
        if not Path(exe).exists():
            raise FileNotFoundError(f"Game exe not found: {exe}")

        app_id = generate_appid(game_name, exe)
        game_path = str(Path(exe).parent)

        # Попытка найти game_id и скачать картинки
        game_id = self.search_game_on_steamgriddb(game_name)
        if game_id:
            logger.info("Найден game_id %s; скачиваем изображения.", game_id)
            self.save_images_to_grid(app_id, game_id, user_id)
        else:
            logger.debug("game_id не найден или API недоступен; пропускаем скачивание картинок.")

        # Подготовка объекта shortcuts
        shortcuts_file = self._user_path(user_id) / "config" / "shortcuts.vdf"
        try:
            shortcuts = load_shortcuts_binary(shortcuts_file)
        except Exception:
            shortcuts = {"shortcuts": {}}

        entry = {
            "appid": app_id,
            "appname": game_name,
            "exe": f'"{exe}"',
            "StartDir": f'"{game_path}"',
            "LaunchOptions": launch_options,
            "IsHidden": 0,
            "AllowDesktopConfig": 1,
            "OpenVR": 0,
            "Devkit": 0,
            "DevkitGameID": "",
            "LastPlayTime": 0,
            "tags": {}
        }

        idx = len(shortcuts.get("shortcuts", {}))
        shortcuts.setdefault("shortcuts", {})[str(idx)] = entry

        # --- Перед записью пытаемся перезапустить Steam, чтобы освободить locks на shortcuts.vdf ---
        variant, restarted = restart_steam_if_running(prompt_before_restart=False, allow_restart=True)
        if restarted:
            logger.info("Steam перезапущен (%s) для освобождения блокировок файлов.", variant)
            time.sleep(2.0)  # дать время процессу подняться/опуститься

        # Сериализация и атомарная запись
        dump_shortcuts_binary(shortcuts_file, shortcuts)
        logger.info("Добавлена игра %s с appid %s для пользователя %s", game_name, app_id, user_id)
        return {"status": "success", "app_id": app_id}


# ---- пользовательский интерфейс (CLI) ----

def choose_user_interactively(adder: NonSteamGameAdder) -> Optional[str]:
    users = adder.get_local_steam_usernames()
    if not users:
        logger.error("Локальные Steam пользователи не найдены в %s", adder.steam_dir)
        return None
    items = list(users.items())
    if len(items) == 1:
        logger.info("Найден единственный пользователь %s (%s)", items[0][1], items[0][0])
        return items[0][0]
    recent = adder.get_current_steam_user()
    if recent:
        logger.info("MostRecent Steam user detected: %s", recent[0])
        return recent[0]
    print("Multiple users detected. Select a user:")
    for i, (uid, name) in enumerate(items, start=1):
        print(f"{i}. {name} ({uid})")
    while True:
        try:
            sel = int(input("> "))
            if 1 <= sel <= len(items):
                return items[sel - 1][0]
        except Exception:
            print("Invalid selection.")


def main():
    try:
        steam_path_env = os.environ.get("STEAM_USERDATA")
        api_key = get_api_key(interactive_save=True)
        adder = NonSteamGameAdder(steam_dir=Path(steam_path_env) if steam_path_env else None, api_key=api_key)

        exe = input("Enter the path to your game executable:\n> ").strip()
        name = input("Enter the name of the game:\n> ").strip()
        launch = input("Enter launch options or press Enter to skip:\n> ").strip()

        if not exe or not name:
            logger.error("Exe path and game name are required.")
            return

        user_id = choose_user_interactively(adder)
        if not user_id:
            logger.error("No Steam user selected; aborting.")
            return

        res = adder.add_non_steam_game(exe, name, user_id, launch)
        logger.info("Result: %s", res)

    except KeyboardInterrupt:
        logger.info("Interrupted by user.")
    except Exception as e:
        logger.exception("Unexpected error: %s", e)


if __name__ == "__main__":
    main()
