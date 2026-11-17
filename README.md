<div align="center">

<img src="pwaatvitalogo.png" alt="Phoenix Wright: Ace Attorney Trilogy PS Vita port logo" width="420">

# Phoenix Wright: Ace Attorney Trilogy
### PlayStation Vita Port

**A port of the PC version of the game, built from its Unity project and brought to the PS Vita.**

</div>

---

## Table of Contents

- [About](#about)
- [Requirements](#requirements)
- [Installation](#installation)
- [Languages](#languages)
- [Plugins](#plugins)
- [Folder Layout](#folder-layout)
- [Troubleshooting](#troubleshooting)
- [Legal](#legal)
- [Credits](#credits)

---

## About

This project ports the PC version of *Phoenix Wright: Ace Attorney Trilogy* to the PlayStation Vita. The PC build runs on Unity; the port was made from its decompiled project, with AI assistance used for help with the code.

The port is **a loader and a set of code changes only**. It does not contain any game assets. All textures, audio, text and other data are extracted from **your own legitimate copy** of the PC game by a tool that runs on your computer.

### What was changed for Vita

- Assets are converted offline on your PC, so the console does no heavy processing while you play
- Textures are resized and compressed to fit the Vita's screen and memory
- Steam features (achievements, cloud saves, online checks) are replaced with local equivalents
- Input is mapped to the Vita's buttons and touch screen

---

## Requirements

| What | Details |
|------|---------|
| Console | PS Vita or PS TV with a homebrew-enabled firmware (such as HENkaku / Enso) |
| Game copy | A legitimately purchased PC copy of *Phoenix Wright: Ace Attorney Trilogy* |
| PC | Windows, to run the extraction tool |
| Storage | Enough free space on `ux0:` for the converted `Media` folder |
| Software | [VitaShell](https://github.com/TheOfficialFloW/VitaShell) or any FTP client to transfer files |

> [!IMPORTANT]
> **You must provide your own game files. They are not included in this project and will never be.**

---

## Installation

### 1. Install the VPK

1. Download the latest `.vpk` from the [**Releases**](../../releases) page.
2. Copy it to your Vita and install it with VitaShell.

### 2. Prepare your game files

1. Make sure you own a licensed PC copy of the game.
2. Download the **extraction tool** (`.exe`) from the same Releases page.
3. Run the tool and point it at the game's `PWAAT_Data` folder.
4. *(Optional)* To play in Russian, download the translation patch by **Failing Forward** from their [guide](https://steamcommunity.com/sharedfiles/filedetails/?id=1845804123) and load it into the extraction tool. It will be run through the conversion together with the game files.
5. Wait for it to finish. It will generate a **`Media`** folder.

> [!NOTE]
> The Russian patch is optional. The game works fully without it, and nothing needs to be copied by hand: the tool converts the patch for you.

### 3. Copy the files to your Vita

Copy the generated `Media` folder to:

```
ux0:app/OBJECTION
```

### 4. Play

Launch the game from the LiveArea. Objection!

---

## Languages

The port includes all **9 languages** from the latest version of the PC game, plus a **10th, built-in Russian translation**.

| # | Language | Source |
|---|----------|--------|
| 1 | English | Original game |
| 2 | Japanese | Original game |
| 3 | French | Original game |
| 4 | German | Original game |
| 5 | Korean | Original game |
| 6 | Simplified Chinese | Original game |
| 7 | Traditional Chinese | Original game |
| 8 | Spanish (Latin America) | Original game |
| 9 | Portuguese (Brazil) | Original game |
| 10 | **Russian** | Fan translation by **Failing Forward** |

### Russian translation

The built-in Russian translation was made by the **Failing Forward** team and is a fan project, not an official localization. You can read more about it, and about the team, here:

**[Failing Forward - Ace Attorney Trilogy translation (Steam guide)](https://steamcommunity.com/sharedfiles/filedetails/?id=1845804123)**

---

## Plugins

The port runs as a normal homebrew application and works without extra plugins. The ones below are **optional** and can improve the experience.

| Plugin | Required? | Purpose |
|--------|-----------|---------|
| `fd_fix` | Recommended | Raises the limit of files a game can have open at once. Helps avoid crashes and loading errors in file-heavy games. |
| `PSVshell` | Optional | On-screen performance monitor and clock/overclock control. Handy for checking frame rate and memory use. |
| `NoTrpDrm` | Optional | Enables trophies for the port, including unofficial ones. See [Trophies](#trophies). |
| `kubridge` | Only if the release notes say so | Kernel helper used by some ports. Install it only if the release page asks for it. |

### Installing a plugin

1. Place the plugin file (`.skprx`) in `ur0:tai/`.
2. Open `ur0:tai/config.txt` and add its path under the `*KERNEL` section, for example:

   ```
   *KERNEL
   ur0:tai/fd_fix.skprx
   ```

3. Reboot your console, or refresh the config via HENkaku settings.

### Trophies

`NoTrpDrm` lets the port use unofficial trophies. Trophy sync with PSN may not work while this plugin is enabled, so to sync your trophies you will need to **disable the plugin**. Keep in mind that without it, the unofficial trophies will not be available.

> [!TIP]
> Always check the release notes. If a specific version needs a particular plugin, it will be listed there.

> [!WARNING]
> Plugins can affect other homebrew and games. Only install ones you trust and remove any you do not need.

---

## Folder Layout

After setup, your Vita should look like this:

```
ux0:
└── app/
    └── OBJECTION/
        ├── eboot.bin          ← installed from the VPK
        ├── sce_sys/           ← installed from the VPK
        └── Media/             ← generated by the extraction tool (you add this)
```

---

## Troubleshooting

<details>
<summary><b>No OBJECTION folder</b></summary>

Make sure you installed the `.vpk` file first.

</details>

<details>
<summary><b>C2-12828-1</b></summary>

Make sure your game has `Media`. If it does, reinstall the game or check your memory card for any issues.

</details>

<details>
<summary><b>The game does not start or closes immediately</b></summary>

- Check that the `Media` folder is located at exactly `ux0:app/OBJECTION/Media`.
- Make sure the copy finished completely. Transfer errors are common over FTP, so verify the folder size against the PC.
- Install `fd_fix` (see [Plugins](#plugins)) and restart the console.

</details>

<details>
<summary><b>The extraction tool shows an error</b></summary>

- Select the `PWAAT_Data` folder itself, not the parent game folder.
- Make sure the game is fully installed and files have not been moved or modified.
- Use a clean, up-to-date copy of the game (verify files in your game launcher if needed).

</details>

<details>
<summary><b>Stutters or frame drops</b></summary>

- Make sure the `Media` folder is stored on a fast memory card. Slow cards can cause hitches when loading scenes.
- Use PSVshell to check the frame rate and adjust the clock if needed.

</details>

---

## Legal

- This project is an **unofficial fan-made port** and is **not affiliated with, endorsed by, or connected to Capcom**.
- *Phoenix Wright: Ace Attorney* and related names, characters and assets are the property of Capcom Co., Ltd.
- **No copyrighted game assets are distributed in this repository or its releases.** The extraction tool works only with files from a copy of the game that you own.
- This project is provided as is, free of charge, for preservation and personal use. Please support the official release.
- Use at your own risk. Modifying your console is your responsibility.

---

## Credits

- **Port & development:** **[RetroBoy](https://github.com/BekaLife08)**
- **[Lucky 13](https://github.com/LuckyI13I)** for graphic design help and testing the port
- **[kayday](https://github.com/kriemhild3ds)** for active game testing and coding help
- **[Failing Forward](https://steamcommunity.com/sharedfiles/filedetails/?id=1845804123)** - for russian translation and graphic help
- **VitaSDK** and the PS Vita homebrew community
- **AssetRipper** and the tools used for analysis and conversion
- Everyone who tested, reported bugs and helped

---

<div align="center">

*If you enjoy the port, buy the original game and support the developers.*

</div>
