````markdown
# Non-Steam Game Adder (Rewritten with AI Assistance)

This repository contains a Python script, **rewritten and modernized with the help of AI**, designed to add non-Steam games to your Steam library while automatically fetching artwork from SteamGridDB. The script is cross-platform, supporting both Linux and Windows, and includes enhanced dependency handling and secure API key management using **Python's `keyring`** module.

---

## Features

- Add any non-Steam game to your Steam library.
- Automatically fetch artwork from SteamGridDB, including:
  - Grid images
  - Wide grids
  - Hero banners
  - Logos
  - Icons (automatically resized to 64x64)
- Detects Steam installation paths on Windows, Linux (both native and Flatpak), and handles multiple user accounts.
- Securely stores and retrieves SteamGridDB API keys using `keyring`.
- Automatic dependency checking and installation (for `requests`, `pillow`, `vdf`, `keyring`, and `secretstorage`).
- Logs detailed info and errors for easy debugging.

---

## Installation

1. Clone the repository:

```bash
git clone https://github.com/Cart1416/Add-Non-Steam-CLI.git
cd Add-Non-Steam-CLI
````

2. Install Python dependencies:

```bash
pip install -r requirements.txt
```

> The script can also attempt to automatically install missing dependencies using `pip` or your system package manager if needed.

---

## Usage

### Interactive CLI

Run the script interactively:

```bash
python main.py
```

The script will prompt you for:

* Path to the game's executable
* Game name
* Optional launch parameters
* SteamGridDB API key (can be stored securely via `keyring`)

The script then:

1. Detects your Steam user accounts.
2. Lets you select which user to add the game to.
3. Generates a unique Steam-compatible `appid`.
4. Fetches artwork from SteamGridDB.
5. Saves images to the appropriate Steam `grid` folder.
6. Updates your Steam `shortcuts.vdf` file.

### Module Usage

The script can be imported as a module in your own projects. For example:

```python
from main import NonSteamGameAdder

# Initialize with a stored SteamGridDB API key
adder = NonSteamGameAdder()

# Fetch images for a game
image_url = adder.fetch_steamgriddb_image(game_id=12345, image_type="grid")

# List local Steam usernames
usernames = adder.get_local_steam_usernames()

# Add a non-Steam game
adder.add_non_steam_game(
    game_exe_path="/path/to/game/executable",
    game_name="My Non-Steam Game",
    user_id="12345678",
    launch_options="-fullscreen"
)
```

> The `keyring` module ensures that API keys are securely stored and retrieved, removing the need to input them on every run.

---

## Dependencies

* **Python 3.7+**
* `requests` – HTTP requests to SteamGridDB API
* `pillow` – Image handling and resizing
* `vdf` – Reading and writing Steam VDF files
* `keyring` – Secure storage of API keys
* `secretstorage` – Backend for `keyring` on Linux

The script also includes automatic checks for these dependencies and can attempt to install them if missing.

---

## Notes

* You must obtain a SteamGridDB API key from [https://www.steamgriddb.com](https://www.steamgriddb.com) for artwork fetching.
* Ensure Steam is installed in the default path or update the Steam directories in the script (`Windows` or `Linux` paths).
* Flatpak Steam users on Linux are supported, with automatic detection of the correct Steam user data directory.
* The rewritten script includes improved logging, exception handling, and modern Python best practices.

---

### Acknowledgment

This version of the script has been **rewritten and enhanced with AI assistance**, improving readability, maintainability, and security features such as API key handling via `keyring`.

#non-steam-adder #steamgriddb #python #keyring #automation
[[Python]] [[Steam]] [[SteamGridDB]] [[CLI]] [[Keyring]] [[Automation]]

```

Если хочешь, я могу сразу сделать **расширенную версию README**, где будет древовидная схема работы скрипта и объяснение каждого шага (для легкого внесения изменений). Это будет особенно удобно для поддержки и коммитов на GitHub.
```
