# iphone-pull

Copy photos and videos from an iPhone to a folder on your computer over USB, with a progress bar. Works on Windows, macOS and Linux.

The "Internal Storage" folder you see in Windows Explorer is not a real path, so normal copy tools can't read it. This script talks to the phone directly using Apple's AFC protocol through [pymobiledevice3](https://github.com/doronz88/pymobiledevice3).

## Features

- Parallel transfers (4 by default) and 4 MB reads
- One progress bar with speed, ETA and file count
- Resumable: files already copied with the same size are skipped
- Each file is written as `.part` and renamed only when complete
- Retries failed files, keeps original timestamps
- Checks free disk space before starting
- Handles Windows paths longer than 260 characters

## Setup

You need Python 3.9 or newer.

```
pip install -r requirements.txt
```

On Windows, also install **Apple Devices** from the Microsoft Store (or iTunes). It provides the USB driver. Then unlock the iPhone, plug it in and tap **Trust This Computer**.

## Usage

Tell the script where to copy from (`--src`, a folder on the phone) and where to copy to (`--dst`, a folder on your computer). `--dst` is required. `--src` defaults to `/DCIM`, the camera roll.

```
python iphone_pull.py --list
python iphone_pull.py --dst D:\iphone --dry-run
python iphone_pull.py --dst D:\iphone
```

Start with `--list` to see which folders the phone exposes. Then run with `--dry-run`, which scans the phone and prints the file count and total size without copying anything. Then run it for real.

| Option | Meaning |
| --- | --- |
| `-s`, `--src PATH` | Folder on the phone. Default `/DCIM`. Use `/` for everything exposed over USB. |
| `-d`, `--dst PATH` | Folder on your computer to copy into. Created if missing. Required. |
| `--list` | List the contents of `--src` (default `/`) and exit. |
| `--workers N` | Parallel transfers. Default 4. |
| `--retries N` | Attempts per file. Default 3. |
| `--udid ID` | Pick a device when several are connected. |
| `--overwrite` | Copy again even if the size already matches. |
| `--dry-run` | Scan and report only. |
| `-v` | Show warnings and debug details. |

The contents of `--src` are copied directly into `--dst`, so `--src /DCIM` puts folders like `100APPLE` straight inside your destination.

Examples:

```
python iphone_pull.py --src /DCIM/100APPLE --dst ~/Pictures/iphone
python iphone_pull.py --src / --dst E:\backup --workers 6
```

If the transfer is interrupted, run the same command again. It picks up where it left off.

## Before you erase the phone

A finished copy does not prove every photo is on your computer. Some items may be only in iCloud, and some data is not visible over USB.

1. Run the script again. It should report `To copy: 0`.
2. In the Photos app, compare the counts under Albums > Media Types with the matching files in your destination folder.
3. Check that the newest and oldest photos, and a long video, open correctly.
4. Keep a second copy elsewhere before deleting anything.

If iCloud Photos is on, deleting on the phone also deletes from iCloud. If Optimize iPhone Storage is on, switch to Download and Keep Originals first, or the copy will be missing those originals.

## Troubleshooting

- **Could not connect:** check the cable, unlock the phone, accept the Trust prompt, and make sure Apple Devices or iTunes is installed on Windows.
- **Total is smaller than iPhone Storage shows:** Storage includes the Photos database and thumbnails, which are not exposed over USB.
- **Some files skipped:** run with `-v` and look for `cannot stat` or `cannot list` warnings.

## Notes

- The script only reads from the phone. It never deletes or changes anything on it.
- Tested against a simulated device. Try a small folder on your own phone first, for example `--src /DCIM/100APPLE --dst <folder>`.
